"""Asistente de voz para el ESP32 (lado servidor: VM en la nube o PC).

Reconocimiento de voz con faster-whisper (o Whisper de Groq para las preguntas),
deteccion de voz con Silero (viene con faster-whisper), voz con Edge TTS + ffmpeg
y respuestas de Gemini por su API REST (con Groq de respaldo si se queda sin cuota).

Este programa escucha en SERVER_PORT y el ESP32 se conecta (la VM no puede entrar
a la red de la casa). Cada conexion empieza con "CATRINA <token> <papel>\n":
  mic: el ESP32 manda el audio del microfono sin parar, PCM 16 kHz / 16 bit / mono
  ctl: recibe el estado de la tira LED ("pensando\n"...), "callar\n" y "hablar\n"
  spk: la abre el ESP32 al recibir "hablar"; trae el audio de la respuesta y se
       cierra al terminar

Al oir "Catrina" siempre contesta "¿Me llamabas?" y despues escucha la pregunta.
Si dicen "Catrina" mientras contesta, corta la respuesta y vuelve a preguntar.
La palabra se busca cada WAKE_WINDOW segundos de voz, sin esperar a que termine
la frase, y acepta como la escribe mal Whisper.
"""
import array
import asyncio
import collections
import faulthandler
import hmac
import io
import json
import math
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.request
import uuid
import wave

import edge_tts
import numpy as np
from faster_whisper import WhisperModel
from faster_whisper.vad import get_vad_model

# ---------- CONFIG ----------
FFMPEG_PATH = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
SERVER_PORT = 5000               # el ESP32 se conecta aqui (abrirlo en el firewall de la VM)
SERVER_BIND = os.getenv("CATRINA_BIND", "")   # "" = todas las redes; 127.0.0.1 para pruebas
TOKEN_FILE = "token.txt"         # contraseña que comparte con el ESP32 (TOKEN en el .ino)
HELLO_TIMEOUT = 5                # segundos para que una conexion nueva diga quien es
SPK_WAIT = 5                     # segundos esperando a que el ESP32 abra la bocina
MIC_TIMEOUT = 5                  # el ESP32 manda audio sin parar: 5 s sin nada = conexion muerta

# ---------- SIN SUPERVISION ----------
TTS_TIMEOUT = 8                  # segundos que espera a Edge TTS por cada frase
MAX_BUSY = 120                   # una conversacion trabada mas de esto reinicia el programa
LOG_FILE = "catrina.log"         # todo lo que imprime, con la hora, para revisar despues
LOG_MAX = 5_000_000              # al pasar de 5 MB el log viejo pasa a catrina.log.old

GEMINI_MODEL = "gemini-3.5-flash-lite"
GEMINI_TIMEOUT = 10              # segundos; si Gemini no contesta, avisa
MAX_TOKENS = 150                 # tope de la respuesta; si se corta a media frase, subelo
# instrucciones minimas: se mandan en cada pregunta, asi que cada palabra cuesta
PROMPT = ("Contesta exactamente lo que te preguntan, con datos concretos y sin rodeos ni "
          "cambiar de tema. Público adulto. Español de México, máximo 50 palabras, sin "
          "markdown ni emojis. Si una palabra parece mal dictada, usa la más probable.")
# claves en orden de uso; las que empiezan con "gsk_" son de Groq (respaldo gratis)
KEY_FILES = ("apikey.txt", "apikey2.txt")
GROQ_MODEL = "openai/gpt-oss-120b"   # el que mejor contesto en español; empieza en ~0.5 s
GROQ_MAX_TOKENS = 500            # incluye lo que "piensa" antes de contestar
# errores que cambian de clave: 429 = sin cuota; 400/401/403 = clave mala o bloqueada;
# 5xx = el servicio esta caido (con la clave de otro servicio si puede contestar)
KEY_ERRORS = (400, 401, 403, 429, 500, 502, 503, 504)
CACHE_FILE = "respuestas.json"   # preguntas repetidas se contestan sin gastar tokens
CACHE_MAX = 500

# preguntas: con clave de Groq se transcriben alla con whisper-large-v3-turbo (~0.5 s,
# mejor que small y sin gastar CPU de la VM); si Groq falla o no hay clave, aqui
GROQ_STT_MODEL = "whisper-large-v3-turbo"
GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
# local: tiny, base, small. small se equivoca en la mitad de palabras que base
# con el ruido de este microfono, a cambio de ~1.3 s mas por pregunta
WHISPER_MODEL = "small"
VOCABULARY = "Catrina."          # ayuda a Whisper a escribir bien la palabra de activacion

# ---------- PALABRA DE ACTIVACION ----------
WAKE_WORD = "catrina"
# letras de diferencia que acepta: con 2 entran Katrina, Catarina, Catalina, Patrina...
# subelo si no la reconoce; bajalo a 1 si se activa sola
WAKE_TOLERANCE = 2
# palabras de verdad que se parecen a "catrina" pero no la activan
NOT_WAKE = {"camina", "caminas", "cantina", "cantinas", "cabina", "cabinas", "canina",
            "caninas", "cansina", "latrina", "catrin", "catrines"}
WAKE_WINDOW = 2.0                # segundos de voz que revisa de una vez (no espera a que termine)
WAKE_SILENCE = 0.35              # silencio que basta para revisar antes
WAKE_OVERLAP = 0.8               # segundos que se repiten entre ventanas seguidas
WAKE_MODEL = "base"              # modelo que busca la palabra
WAKE_HOTWORDS = "Catrina"        # le dice a Whisper que espere esta palabra
MAX_LAG = 1.5                    # si el reconocimiento se atrasa, salta audio viejo
MIC_MAX_GAIN = 20                # ganancia maxima para voces lejanas; bajala si oye ruido
MIC_TARGET = 0.5                 # nivel al que se normaliza la voz antes de Whisper

SAMPLE_RATE = 16000
VOICE = "es-MX-DaliaNeural"
VOICE_RATE = "-5%"               # velocidad de la voz (Edge TTS)
VOLUME = 1.0                     # 0.0 a 1.0; bajalo si la bocina distorsiona
BOOST = 2.5                      # ganancia extra tras comprimir (1 = sin extra, max ~3)

# segundos de silencio que marcan el fin de la pregunta; con menos, una pausa normal
# ("¿cual es la capital... de Francia?") la partia y solo se mandaba la mitad
END_SILENCE = 1.0
MAX_UTTERANCE = 12               # tope de una frase, por si hay ruido constante
MIN_UTTERANCE = 0.3              # segundos de voz; frases mas cortas se ignoran (golpes, chasquidos)
START_TIMEOUT = 5                # tras "¿Me llamabas?", tiempo para empezar a hablar
VAD_THRESHOLD = 0.5              # 0-1: que tan seguro debe estar Silero de que es voz
# volumen extra solo para Silero: el microfono llega bajito y sin esto no detectaba
# voces suaves o lejanas; con 8 ya confunde el zumbido del microfono con voz
VAD_GAIN = 4

NIGHT = (22, 7)                  # de 22:00 a 7:00 la tira usa el modo nocturno; None = nunca

GREETING = "¿Me llamabas?"
NOT_HEARD = "No te escuché."
ERROR_REPLY = "No pude responder. Intenta de nuevo en un momento."
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BYTES_PER_SEC = SAMPLE_RATE * 2
FRAME = 1024                     # 512 muestras = 32 ms, el bloque que usa Silero
PRE_FRAMES = 10                  # 0.32 s de audio antes de que empiece la voz
GEMINI_URL = (f"https://generativelanguage.googleapis.com/v1beta/models/"
              f"{GEMINI_MODEL}:streamGenerateContent?alt=sse")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
FFMPEG_CMD = [FFMPEG_PATH, "-loglevel", "error", "-f", "mp3", "-i", "pipe:0",
              # sin graves (la bocina no los da y distorsionan), voz comprimida y limitador
              "-af", f"highpass=f=180,acompressor=threshold=-24dB:ratio=4:attack=5:"
                     f"release=120:makeup=3,volume={VOLUME * BOOST},"
                     f"alimiter=limit=0.95:attack=2:release=40:level=0",
              "-f", "s16le", "-acodec", "pcm_s16le",
              "-ar", str(SAMPLE_RATE), "-ac", "1", "pipe:1"]


class ApiKeys:
    """Claves en orden (Gemini o Groq): si una se queda sin cuota, sigue con la
    siguiente (y se queda en ella para las preguntas que vienen)."""

    def __init__(self):
        self.keys = [os.getenv("GEMINI_API_KEY")] if os.getenv("GEMINI_API_KEY") else []
        for name in KEY_FILES:
            try:
                with open(os.path.join(BASE_DIR, name), encoding="utf-8") as f:
                    key = f.read().strip().split("=")[-1].strip().strip('"')
            except OSError:
                continue
            if key and key not in self.keys:
                self.keys.append(key)
        if not self.keys:
            raise SystemExit(f"No hay clave de Gemini: ponla en {KEY_FILES[0]}")
        print("Claves: " + ", ".join(self.service(k) for k in self.keys))
        self.i = 0

    @staticmethod
    def service(key):
        return "Groq" if key.startswith("gsk_") else "Gemini"

    def current(self):
        return self.keys[self.i]

    def next(self):
        self.i = (self.i + 1) % len(self.keys)

    def groq(self):
        return next((k for k in self.keys if self.service(k) == "Groq"), None)


def is_night():
    if not NIGHT:
        return False
    start, end = NIGHT
    hour = time.localtime().tm_hour
    return start <= hour or hour < end if start > end else start <= hour < end


def idle_state():
    return "nocturno" if is_night() else "reposo"


# ---------- CONEXION CON EL ESP32 ----------

def load_token():
    token = os.getenv("CATRINA_TOKEN")
    if not token:
        try:
            with open(os.path.join(BASE_DIR, TOKEN_FILE), encoding="utf-8") as f:
                token = f.read().strip()
        except OSError:
            pass
    if not token:
        raise SystemExit(f"Falta {TOKEN_FILE}: la contraseña que comparte con el ESP32")
    return token


class Esp32Link:
    """Recibe las conexiones del ESP32 (ver arriba: mic, ctl y spk).

    Una conexion nueva del mismo papel reemplaza a la vieja: si el ESP32 se
    reinicio, la vieja quedo colgada. Las que no traen el token se cierran."""

    def __init__(self, token):
        self.token = token.encode()
        self.on_mic = None               # lo pone MicStream
        self.ctl = None
        self.ctl_lock = threading.Lock()
        self.speakers = queue.Queue()

    def start(self):
        # despues de crear MicStream: antes, el microfono del ESP32 no tenia quien lo
        # recibiera y su conexion se cerraba sin avisar
        srv = socket.create_server((SERVER_BIND, SERVER_PORT))
        srv.listen(8)
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()
        print(f"Esperando al ESP32 en el puerto {SERVER_PORT}...")

    def _accept(self, srv):
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self._hello, args=(conn, addr), daemon=True).start()

    def _hello(self, conn, addr):
        # primera linea: "CATRINA <token> <papel> [datos]"; lo que siga ya es audio
        conn.settimeout(HELLO_TIMEOUT)
        buf = b""
        try:
            while b"\n" not in buf and len(buf) < 200:
                data = conn.recv(200)
                if not data:
                    break
                buf += data
        except OSError:
            pass
        line, _, rest = buf.partition(b"\n")
        parts = line.decode(errors="replace").split(" ", 3)
        if len(parts) < 3 or parts[0] != "CATRINA" or \
                not hmac.compare_digest(parts[1].encode(), self.token):
            print(f"(conexion rechazada de {addr[0]}: no es el ESP32)")
            conn.close()
            return
        role, extra = parts[2], parts[3] if len(parts) > 3 else ""
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if role == "mic":
            self.on_mic(conn, rest, addr, extra)
        elif role == "ctl":
            with self.ctl_lock:
                if self.ctl:
                    self.ctl.close()
                conn.settimeout(2)
                self.ctl = conn
        elif role == "spk":
            conn.settimeout(10)
            self.speakers.put(conn)
        else:
            print(f"(conexion de {addr[0]} con papel desconocido: {role})")
            conn.close()

    def send_ctl(self, line):
        """Manda una linea al ESP32 ("pensando", "callar"...). False si no hay conexion."""
        with self.ctl_lock:
            if not self.ctl:
                return False
            try:
                self.ctl.sendall(f"{line}\n".encode())
                return True
            except OSError:
                self.ctl.close()
                self.ctl = None
                return False

    def open_speaker(self):
        """Le pide al ESP32 que abra la bocina y devuelve esa conexion."""
        while not self.speakers.empty():   # sobrantes de una vez que no se usaron
            self.speakers.get_nowait().close()
        if not self.send_ctl("hablar"):
            raise OSError("el ESP32 no esta conectado")
        try:
            return self.speakers.get(timeout=SPK_WAIT)
        except queue.Empty:
            raise OSError(f"el ESP32 no abrio la bocina en {SPK_WAIT} s") from None


# ---------- MICROFONO ----------

class MicStream:
    """Recibe audio del ESP32 en segundo plano y lo corta en frases.

    Detecta la voz con Silero (una red que reconoce voz humana sin importar el
    volumen): asi no se le escapan las voces lejanas o bajitas."""

    def __init__(self, link):
        self.q = queue.Queue()
        self.rest = bytearray()
        self.vad = get_vad_model().session
        # memoria de Silero entre bloques: estado de la red y los ultimos 64 samples
        self.h = np.zeros((1, 1, 128), np.float32)
        self.c = np.zeros((1, 1, 128), np.float32)
        self.ctx = np.zeros(64, np.float32)
        self.link = link
        self.conn = None
        self.connected = False
        link.on_mic = self._serve

    def _serve(self, conn, first, addr, extra):
        if self.conn:   # el ESP32 se reconecto: la conexion vieja ya no sirve
            self.conn.close()
        self.conn = conn
        # sin tope, si el ESP32 se reinicia o pierde el WiFi sin cerrar la conexion
        # esto esperaba para siempre: Catrina ya no oia nada y parecia congelada
        conn.settimeout(MIC_TIMEOUT)
        self.connected = True
        print(f"ESP32 conectado desde {addr[0]}" + (f" ({extra})" if extra else ""))
        if first:
            self.q.put((time.time(), first))
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except socket.timeout:
                    print(f"(el ESP32 no manda audio hace {MIC_TIMEOUT} s)")
                    break
                except OSError:
                    break
                if not data:
                    break
                self.q.put((time.time(), data))
        if self.conn is conn:
            self.connected = False
            print("ESP32 desconectado")

    def flush(self):
        while not self.q.empty():
            self.q.get_nowait()
        self.rest.clear()

    def _is_speech(self, frame):
        x = np.clip(np.frombuffer(frame, np.int16).astype(np.float32) * (VAD_GAIN / 32768), -1, 1)
        prob, self.h, self.c = self.vad.run(
            None, {"input": np.concatenate([self.ctx, x])[None], "h": self.h, "c": self.c})
        self.ctx = x[-64:]
        return float(np.ravel(prob)[0]) > VAD_THRESHOLD

    def _frame(self):
        """Siguientes 32 ms de audio, o None si no llega nada en 0.1 s."""
        while len(self.rest) < FRAME:
            try:
                arrived, data = self.q.get(timeout=0.1)
            except queue.Empty:
                return None
            if time.time() - arrived > MAX_LAG:   # atrasado: ir al audio de ahora
                self.rest.clear()
                continue
            self.rest.extend(data)
        frame = bytes(self.rest[:FRAME])
        del self.rest[:FRAME]
        return frame

    def next_utterance(self, start_timeout=None, max_len=MAX_UTTERANCE,
                       end_silence=END_SILENCE, stop=None):
        """Espera a que alguien hable y devuelve la frase (PCM) al detectar silencio
        o al juntar max_len segundos.

        Devuelve None si pasa start_timeout sin que nadie hable o si `stop` se activa."""
        pre = collections.deque(maxlen=PRE_FRAMES)   # audio antes de la voz: no cortar el inicio
        voiced = bytearray()
        talking = False
        loud = silent = lead = 0
        start = time.time()
        while True:
            if stop and stop.is_set():
                return None
            frame = self._frame()
            if not talking and start_timeout and time.time() - start > start_timeout:
                return None
            if frame is None:
                continue
            speech = self._is_speech(frame)
            if not talking:
                pre.append(frame)
                loud = loud + 1 if speech else 0
                if loud >= 2:    # 64 ms seguidos de voz
                    talking = True
                    voiced = bytearray(b"".join(pre))
                    lead = len(voiced) - loud * FRAME   # audio de antes: no cuenta como voz
                    silent = 0
                continue
            voiced.extend(frame)
            silent = 0 if speech else silent + 1
            if len(voiced) >= max_len * BYTES_PER_SEC:
                return bytes(voiced)
            if silent * FRAME >= end_silence * BYTES_PER_SEC:
                # sin contar el audio de antes: un golpe no pasa por frase y Whisper,
                # que espera "Catrina", no la inventa con ruido
                if len(voiced) - lead - silent * FRAME >= MIN_UTTERANCE * BYTES_PER_SEC:
                    return bytes(voiced)
                talking = False   # fue un golpe o un chasquido: seguir esperando
                pre.clear()
                loud = 0


class Leds:
    """Le avisa al ESP32 el estado para que la tira LED lo muestre.

    Mientras suena audio el ESP32 ya hace que la tira siga la voz; esto cubre
    lo que el ESP32 no sabe. Si falla no pasa nada: la voz sigue funcionando."""

    def __init__(self, link):
        self.link = link
        self.state = None
        threading.Thread(target=self._keep_idle, daemon=True).start()

    def set(self, state):
        if state != "advertencia":   # la advertencia es pasajera: no cambia el estado
            self.state = state
        self.send(state)

    def send(self, line):
        """Manda una linea al ESP32 ("pensando", "callar"...)."""
        self.link.send_ctl(line)

    def idle(self):
        self.set(idle_state())

    def _keep_idle(self):
        # cambia a nocturno (o de vuelta) a la hora, y lo repite por si el ESP32 se reinicio
        while True:
            time.sleep(30)
            if self.state in ("reposo", "nocturno"):
                self.idle()


# ---------- RECONOCIMIENTO DE VOZ ----------

def normalize(audio):
    """Sube el volumen de voces lejanas o bajitas antes de Whisper (ganancia automatica)."""
    audio = audio - audio.mean()
    peak = np.percentile(np.abs(audio), 99.9)   # ignora chasquidos sueltos
    gain = min(MIC_MAX_GAIN, MIC_TARGET / max(peak, 1e-6))
    return np.clip(audio * gain, -1.0, 1.0).astype(np.float32)


class Whisper:
    def __init__(self, model, max_tokens=None, hotwords=None):
        self.max_tokens = max_tokens
        self.hotwords = hotwords
        opts = dict(device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 4)
        try:
            # primero el que ya esta descargado: sin internet no se queda esperando a la red
            self.model = WhisperModel(model, local_files_only=True, **opts)
        except Exception:
            self.model = WhisperModel(model, **opts)
        self(b"\0" * BYTES_PER_SEC)   # calentar el modelo

    def __call__(self, pcm):
        audio = normalize(np.frombuffer(pcm, np.int16).astype(np.float32) / 32768)
        segments, _ = self.model.transcribe(audio, language="es", initial_prompt=VOCABULARY,
                                            hotwords=self.hotwords, beam_size=1,
                                            condition_on_previous_text=False,
                                            without_timestamps=True,
                                            max_new_tokens=self.max_tokens)
        return " ".join(s.text for s in segments).strip()


class GroqWhisper:
    """Transcribe las preguntas en Groq; si falla, con el Whisper de aqui."""

    def __init__(self, key, local):
        self.key = key
        self.local = local

    def __call__(self, pcm):
        try:
            return self._groq(pcm)
        except Exception as e:
            print(f"(Whisper de Groq fallo: {e}; uso el de aqui)")
            return self.local(pcm)

    def _groq(self, pcm):
        audio = normalize(np.frombuffer(pcm, np.int16).astype(np.float32) / 32768)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(SAMPLE_RATE)
            f.writeframes((audio * 32767).astype(np.int16).tobytes())
        # formulario multipart a mano (urllib no lo trae)
        sep = uuid.uuid4().hex
        body = b"".join(
            f'--{sep}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            for k, v in (("model", GROQ_STT_MODEL), ("language", "es"),
                         ("prompt", VOCABULARY), ("response_format", "json")))
        body += (f'--{sep}\r\nContent-Disposition: form-data; name="file"; '
                 f'filename="pregunta.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode()
        body += buf.getvalue() + f"\r\n--{sep}--\r\n".encode()
        req = urllib.request.Request(GROQ_STT_URL, body, {
            "Authorization": f"Bearer {self.key}", "User-Agent": "catrina",
            "Content-Type": f"multipart/form-data; boundary={sep}"})
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as r:
            return json.load(r)["text"].strip()


def load_stt(keys):
    """Devuelve (reconocedor de preguntas, reconocedor de la palabra de activacion)."""
    print("Cargando reconocimiento de voz...")
    stt = Whisper(WHISPER_MODEL)
    if keys.groq():
        print(f"Preguntas: {GROQ_STT_MODEL} en Groq (respaldo: {WHISPER_MODEL} aqui)")
        stt = GroqWhisper(keys.groq(), stt)
    # la palabra: pocas letras (max_tokens corto = rapido) y avisandole que viene "Catrina"
    # 32 tokens alcanzan para una ventana entera: con menos, mientras Catrina habla
    # su propia voz llenaba el tope y la palabra al final de la ventana se perdia
    wake = Whisper(WAKE_MODEL, max_tokens=32, hotwords=WAKE_HOTWORDS)
    return stt, wake


class WakeListener:
    """Corta la voz en ventanas de WAKE_WINDOW s para buscar la palabra sin esperar
    a que termine la frase. Si una ventana se corto a media voz, la siguiente
    empieza con su ultimo pedazo: asi no se pierde una "Catrina" partida en dos."""

    def __init__(self, mic, stt):
        self.mic = mic
        self.stt = stt
        self.tail = b""

    def reset(self):
        """Olvida el pedazo guardado: al cambiar de momento (antes o despues de una
        respuesta) ya no es la continuacion de nada y podria traer un "Catrina" viejo."""
        self.tail = b""

    def listen(self, stop=None):
        """Texto de la siguiente ventana de voz ("" si fallo), o None si `stop` se activa."""
        pcm = self.mic.next_utterance(max_len=WAKE_WINDOW, end_silence=WAKE_SILENCE, stop=stop)
        if pcm is None:
            return None
        audio, self.tail = self.tail + pcm, b""
        if len(pcm) >= WAKE_WINDOW * BYTES_PER_SEC:   # cortada a media voz
            self.tail = pcm[-int(WAKE_OVERLAP * BYTES_PER_SEC) // 2 * 2:]
        try:
            return self.stt(audio)
        except Exception as e:
            print(f"Error del reconocimiento de voz: {e}")
            return ""


def plain(text):
    """minusculas sin acentos"""
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def edit_distance(a, b):
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def has_wake_word(text):
    """¿Dijeron "Catrina"? Acepta como la escribe mal Whisper (Katrina, Catarina,
    "cat rina"...): palabras o pares de palabras con pocas letras de diferencia."""
    words = re.findall(r"[a-zñ]+", plain(text).replace("k", "c"))
    wake = plain(WAKE_WORD).replace("k", "c")
    for w in words + [a + b for a, b in zip(words, words[1:])]:
        if abs(len(w) - len(wake)) > WAKE_TOLERANCE or w in NOT_WAKE:
            continue
        d = edit_distance(w, wake)
        # con 1 letra de diferencia siempre; con mas, solo si empieza igual ("ca...")
        if d <= min(1, WAKE_TOLERANCE) or (d <= WAKE_TOLERANCE and w[:2] == wake[:2]):
            return True
    return False



# ---------- GEMINI / GROQ ----------

SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
# gpt-oss escribe espacios y guiones especiales que la consola de Windows no imprime
ODD_CHARS = str.maketrans({" ": " ", " ": " ", " ": " ",
                           "‑": "-", "‐": "-", "*": None})


def gemini_request(key, system, question):
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": question}]}],
        "generationConfig": {
            "maxOutputTokens": MAX_TOKENS,
            "thinkingConfig": {"thinkingLevel": "minimal"},   # sin "pensar": responde antes
        },
    }
    return urllib.request.Request(GEMINI_URL, json.dumps(body).encode(), {
        "Content-Type": "application/json", "x-goog-api-key": key})


def gemini_text(event):
    return "".join(part.get("text", "")
                   for cand in event.get("candidates", [])
                   for part in cand.get("content", {}).get("parts", [])
                   if not part.get("thought"))


def groq_request(key, system, question):
    body = {
        "model": GROQ_MODEL, "stream": True, "max_completion_tokens": GROQ_MAX_TOKENS,
        "reasoning_effort": "low",   # piensa poco: responde antes
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": question}],
    }
    # sin User-Agent, Groq rechaza a urllib
    return urllib.request.Request(GROQ_URL, json.dumps(body).encode(), {
        "Content-Type": "application/json", "Authorization": f"Bearer {key}",
        "User-Agent": "catrina"})


def groq_text(event):
    # lo que "piensa" llega aparte, en "reasoning", y no se dice
    return "".join(c.get("delta", {}).get("content") or "" for c in event.get("choices", []))


def llm_sentences(keys, question):
    """Va entregando la respuesta frase por frase, conforme Gemini (o Groq) la genera."""
    # con la fecha calcula bien edades, aniversarios y "cuanto falta para..."
    system = f"{PROMPT} Hoy es {time.strftime('%d/%m/%Y')}."
    for attempt in range(len(keys.keys)):
        groq = keys.service(keys.current()) == "Groq"
        request, text_of = (groq_request, groq_text) if groq else (gemini_request, gemini_text)
        try:
            resp = urllib.request.urlopen(request(keys.current(), system, question),
                                          timeout=GEMINI_TIMEOUT)
            break
        except (urllib.error.URLError, TimeoutError) as e:
            # el error llega antes de cualquier texto: se puede repetir con otra clave.
            # Tambien si tardo demasiado: el otro servicio puede estar bien
            code = getattr(e, "code", None)
            if (code is not None and code not in KEY_ERRORS) or attempt == len(keys.keys) - 1:
                raise
            keys.next()
            print(f"(fallo la clave {attempt + 1}: {code or e}; "
                  f"sigo con {keys.service(keys.current())})")
    pending = ""
    with resp as r:
        for line in r:
            if not line.startswith(b"data:") or line.strip() == b"data: [DONE]":
                continue
            pending += text_of(json.loads(line[5:])).translate(ODD_CHARS)
            *done, pending = SENTENCE_END.split(pending)
            yield from (s for s in done if s.strip())
    if pending.strip():
        yield pending.strip()


class AnswerCache:
    """Guarda las respuestas en disco: la misma pregunta no vuelve a gastar tokens."""

    def __init__(self):
        self.path = os.path.join(BASE_DIR, CACHE_FILE)
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    @staticmethod
    def key(question):
        plain = unicodedata.normalize("NFD", question.lower())
        plain = "".join(c for c in plain if unicodedata.category(c) != "Mn")
        return " ".join(re.findall(r"\w+", plain))

    def get(self, question):
        return self.data.get(self.key(question))

    def put(self, question, answer):
        self.data[self.key(question)] = answer
        while len(self.data) > CACHE_MAX:
            del self.data[next(iter(self.data))]   # quitar la mas vieja
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=0)
        os.replace(tmp, self.path)


# ---------- VOZ ----------

def make_chime():
    """"Tin-tin" corto que avisa que ya te oyo."""
    samples = array.array("h")
    for freq, dur in ((1047, 0.12), (1568, 0.22)):
        for i in range(int(SAMPLE_RATE * dur)):
            t = i / SAMPLE_RATE
            v = math.sin(2 * math.pi * freq * t) * math.exp(-t * 18) * 0.6 * VOLUME
            samples.append(int(v * 32767))
    return samples.tobytes()


def tts_worker(text, out):
    """Edge TTS de una frase; deja los trozos de mp3 en `out` y al final None.
    Si falla antes de mandar audio lo intenta otra vez."""
    sent = []

    async def run():
        # por defecto Edge TTS espera hasta 60 s: Catrina se quedaba muda y la luz fija
        async for chunk in edge_tts.Communicate(text, VOICE, rate=VOICE_RATE,
                                                connect_timeout=TTS_TIMEOUT,
                                                receive_timeout=TTS_TIMEOUT).stream():
            if chunk["type"] == "audio":
                sent.append(1)
                out.put(chunk["data"])
    try:
        for attempt in range(2):
            try:
                asyncio.run(run())
                return
            except Exception as e:
                print(f"Error de Edge TTS: {e!r}")
                if sent:
                    return
    finally:
        out.put(None)


def text_to_pcm(text):
    out = queue.Queue()
    tts_worker(text, out)
    mp3 = b"".join(iter(out.get, None))
    return subprocess.run(FFMPEG_CMD, input=mp3, capture_output=True, check=True).stdout


def wait_playback(started, n_bytes):
    # esperar a que termine de sonar antes de volver a escuchar
    remaining = started + n_bytes / BYTES_PER_SEC - time.time()
    time.sleep(max(0.0, remaining) + 0.3)


def play_pcm(link, pcm, wait=True):
    with link.open_speaker() as s:
        started = time.time()
        s.sendall(pcm)
    if wait:
        wait_playback(started, len(pcm))


def play_pcm_quiet(link, pcm):
    # para el "tin" en segundo plano: si falla, solo se anota
    try:
        play_pcm(link, pcm, wait=False)
    except OSError as e:
        print(f"Error reproduciendo en el ESP32: {e}")


def speak(link, sentences, after=None, said=None, stop=None):
    """Convierte las frases a voz y las manda al ESP32 conforme salen.

    Cada frase se sintetiza en paralelo y suena en orden, sin esperar al resto.
    Las frases que va diciendo se agregan a `said`. Si `stop` se activa, corta en seco.
    Devuelve (hora en que empezo a sonar o None, error de las frases o None)."""
    proc = subprocess.Popen(FFMPEG_CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    state = {"sock": None, "started": None, "sent": 0, "error": None, "done": False}
    said = [] if said is None else said
    stop = stop or threading.Event()
    failure = []

    def cutter():
        # al interrumpir: soltar la bocina y ffmpeg aunque esten bloqueados escribiendo
        while not stop.wait(0.05):
            if state["done"]:
                return
        if state["sock"]:
            try:
                state["sock"].shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        proc.kill()

    threading.Thread(target=cutter, daemon=True).start()

    def pump():
        try:
            while not stop.is_set() and (chunk := proc.stdout.read1(4096)):
                if state["sock"] is None:
                    if after:
                        after.join()   # que termine el "tin" antes de hablar
                    # se conecta hasta tener audio: el ESP32 corta tras 3 s sin datos
                    state["sock"] = link.open_speaker()
                    state["started"] = time.time()
                state["sock"].sendall(chunk)
                state["sent"] += len(chunk)
        except Exception as e:
            state["error"] = e
            proc.stdout.read()   # vaciar para que ffmpeg no se bloquee

    pumper = threading.Thread(target=pump, daemon=True)
    pumper.start()

    order = queue.Queue()

    def say(text):
        out = queue.Queue()
        threading.Thread(target=tts_worker, args=(text, out), daemon=True).start()
        order.put((text, out))

    def produce():
        try:
            for s in sentences:
                if stop.is_set():
                    break
                said.append(s)
                say(s)
        except Exception as e:
            failure.append(e)
            if not said:
                say(ERROR_REPLY)
        order.put(None)

    threading.Thread(target=produce, daemon=True).start()

    def take(q):
        # como q.get(), pero suelta si interrumpen
        while not stop.is_set():
            try:
                return q.get(timeout=0.05)
            except queue.Empty:
                pass
        return None

    try:
        for sentence, out in iter(lambda: take(order), None):
            print(f"Asistente: {sentence}")
            for data in iter(lambda: take(out), None):
                proc.stdin.write(data)
    except OSError:
        if not stop.is_set():   # ffmpeg se cerro solo: es un error de verdad
            raise
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        pumper.join()
        proc.wait()
        state["done"] = True
        if state["sock"]:
            state["sock"].close()
    if stop.is_set():
        pass
    elif state["error"]:
        print(f"Error reproduciendo en el ESP32: {state['error']}")
    elif state["started"]:
        # esperar a que termine de sonar, atento a una interrupcion
        remaining = state["started"] + state["sent"] / BYTES_PER_SEC - time.time()
        stop.wait(max(0.0, remaining) + 0.3)
    return state["started"], failure[0] if failure else None


def say_cached(mic, fixed, text):
    print(f"Asistente: {text}")
    try:
        if text not in fixed:   # no se pudo preparar al iniciar (¿sin internet?): otra vez
            fixed[text] = text_to_pcm(text)
        play_pcm(mic.link, fixed[text])
    except Exception as e:
        print(f"Error reproduciendo en el ESP32: {e}")
    mic.flush()


def watch_for_wake(wake, leds, said, finished, interrupt):
    """Mientras contesta, escucha por si dicen la palabra para cortarla."""
    while not finished.is_set():
        text = wake.listen(stop=finished)
        if text is None:
            return
        # el microfono tambien oye la bocina: si la respuesta dice "Catrina", no cuenta
        if has_wake_word(text) and not has_wake_word(" ".join(said)):
            print(f"(interrumpido: {text})")
            # ya mismo: esperar a que se cierre todo lo de la respuesta tardaba ~1 s mas
            leds.send("callar")
            interrupt.set()
            return


def answer(mic, leds, keys, fixed, cache, wake, question, heard_at):
    """Contesta la pregunta. Devuelve True si la interrumpieron con la palabra."""
    print(f"Tu: {question}")
    leds.set("pensando")
    chime = threading.Thread(target=play_pcm_quiet, args=(mic.link, fixed["chime"]),
                             daemon=True)
    chime.start()
    saved = cache.get(question)
    if saved:
        print("(respuesta guardada: 0 tokens)")
        sentences = [s for s in SENTENCE_END.split(saved) if s.strip()]
    else:
        sentences = llm_sentences(keys, question)

    said = []
    finished, interrupt = threading.Event(), threading.Event()
    mic.flush()
    wake.reset()   # si no, el "Catrina" que la activo se cuela y la corta sola
    watcher = threading.Thread(target=watch_for_wake, daemon=True,
                               args=(wake, leds, said, finished, interrupt))
    watcher.start()
    started, error = speak(mic.link, sentences, after=chime, said=said, stop=interrupt)
    finished.set()
    watcher.join()
    if interrupt.is_set():
        mic.flush()
        return True
    if error:
        print(f"Error de la IA (Gemini/Groq): {error}")
        # sin internet: error (parpadeo rojo); cuota agotada u otro fallo: advertencia
        offline = isinstance(error, urllib.error.URLError) and \
            not isinstance(error, urllib.error.HTTPError)
        leds.set("error" if offline else "advertencia")
        if offline:
            time.sleep(3)
    elif said and not saved:
        try:
            cache.put(question, " ".join(said))
        except OSError as e:
            print(f"No se pudo guardar la respuesta: {e}")
    if not started:
        # no sono nada (¿Edge TTS caido?): la frase de error ya esta lista en audio
        say_cached(mic, fixed, ERROR_REPLY)
    if started:
        print(f"(empezo a hablar {started - heard_at:.1f} s despues de que terminaste)")
    leds.idle()
    mic.flush()
    return False


def converse(mic, leds, stt, wake, keys, fixed, cache, watchdog):
    """Tras oir la palabra: "¿Me llamabas?", escucha la pregunta y contesta.
    Si la interrumpen con la palabra, vuelve a empezar."""
    while True:
        watchdog.busy()
        say_cached(mic, fixed, GREETING)
        leds.set("escuchando")
        pcm = mic.next_utterance(start_timeout=START_TIMEOUT)
        try:
            question = stt(pcm) if pcm is not None else ""
        except Exception as e:
            print(f"Error del reconocimiento de voz: {e}")
            question = ""
        heard_at = time.time()
        if len(re.findall(r"\w+", question)) < 2:   # nada, o solo "eh", "Catrina"...
            leds.set("advertencia")
            say_cached(mic, fixed, NOT_HEARD)
            leds.idle()
            return
        if not answer(mic, leds, keys, fixed, cache, wake, question, heard_at):
            return


# ---------- SIN SUPERVISION ----------

class Log:
    """Copia lo que se imprime a catrina.log con la hora: sin nadie viendo la
    ventana, es la forma de saber despues que paso."""

    def __init__(self, console):
        self.console = console
        self.line_start = True

    def write(self, s):
        try:
            self.console.write(s)
        except Exception:
            pass
        out = []
        for part in s.splitlines(True):
            if self.line_start:
                out.append(time.strftime("%d/%m %H:%M:%S "))
            out.append(part)
            self.line_start = part.endswith("\n")
        try:
            LOG.write("".join(out))
        except Exception:
            pass

    def flush(self):
        try:
            self.console.flush()
        except Exception:
            pass


def start_log():
    global LOG
    path = os.path.join(BASE_DIR, LOG_FILE)
    try:
        if os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + ".old")
    except OSError:
        pass
    LOG = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout, sys.stderr = Log(sys.stdout), Log(sys.stderr)
    faulthandler.enable(file=LOG)   # si el programa truena feo, deja donde fue


def unattended_windows():
    """Ajustes de Windows para que nada pause al programa sin que nadie lo vea."""
    if os.name != "nt":
        return
    import ctypes
    k = ctypes.windll.kernel32
    # que la PC no se suspenda mientras Catrina corre (la pantalla si se puede apagar)
    k.SetThreadExecutionState(0x80000000 | 0x00000001)   # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    # "Edicion rapida": un clic en la ventana deja el programa pausado hasta pulsar
    # una tecla. Se ve igual que congelado: sin voz y con la luz fija
    handle = k.GetStdHandle(-10)   # STD_INPUT_HANDLE
    mode = ctypes.c_uint32()
    if k.GetConsoleMode(handle, ctypes.byref(mode)):
        k.SetConsoleMode(handle, (mode.value & ~0x0040) | 0x0080)   # sin QUICK_EDIT


class Watchdog:
    """Si una conversacion lleva mas de MAX_BUSY s, algo se trabo: deja en el log
    donde estaba cada parte del programa y lo cierra; iniciar_catrina.bat lo abre
    de nuevo."""

    def __init__(self):
        self.since = None
        threading.Thread(target=self._run, daemon=True).start()

    def busy(self):
        self.since = time.time()

    def done(self):
        self.since = None

    def _run(self):
        while True:
            time.sleep(5)
            if self.since and time.time() - self.since > MAX_BUSY:
                print(f"(trabada mas de {MAX_BUSY} s: reinicio el programa)")
                faulthandler.dump_traceback(file=LOG, all_threads=True)
                LOG.flush()
                os._exit(3)


# ---------- PRINCIPAL ----------

def main():
    start_log()
    unattended_windows()
    print("=== Catrina iniciando ===")
    keys = ApiKeys()
    link = Esp32Link(load_token())
    mic = MicStream(link)
    leds = Leds(link)
    link.start()
    stt, wake_stt = load_stt(keys)
    cache = AnswerCache()

    print("Preparando frases fijas...")
    # las frases fijas se convierten a audio una sola vez y suenan al instante
    fixed = {"chime": make_chime()}
    for phrase in (GREETING, NOT_HEARD, ERROR_REPLY):
        try:
            fixed[phrase] = text_to_pcm(phrase)
        except Exception as e:
            print(f"No se pudo preparar '{phrase}': {e}")

    while not mic.connected:
        time.sleep(0.2)
    leds.idle()
    print('Listo. Di "Catrina".')

    wake = WakeListener(mic, wake_stt)
    watchdog = Watchdog()
    while True:
        # revisa cada WAKE_WINDOW s de voz sin esperar a que termine la frase
        text = wake.listen()
        if not text:
            continue
        print(f"(oido) {text}")
        # siempre confirma y escucha la pregunta aparte: lo que venia junto a
        # "Catrina" suele ser ruido o un saludo y no se manda a Gemini
        if has_wake_word(text):
            try:
                converse(mic, leds, stt, wake, keys, fixed, cache, watchdog)
            except Exception:
                # un error raro no debe apagar a Catrina: se anota y sigue escuchando
                print("Error inesperado (sigo escuchando):")
                traceback.print_exc()
                leds.idle()
                mic.flush()
            watchdog.done()
            wake.reset()   # lo guardado es de la respuesta, no de lo que sigue


if __name__ == "__main__":
    main()
