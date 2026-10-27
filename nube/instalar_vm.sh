#!/bin/bash
# Instala o actualiza Catrina en la VM (Debian 12), desde el repositorio clonado:
#   sudo bash catrina/nube/instalar_vm.sh
# Las claves no estan en GitHub: la primera vez las pide y se quedan en la VM.
set -euo pipefail

ORIGEN="$(cd "$(dirname "$0")/.." && pwd)"
DESTINO=/opt/catrina

echo "=== Paquetes del sistema ==="
apt-get update -q
apt-get install -y -q python3-venv ffmpeg git

id catrina >/dev/null 2>&1 || useradd --system --home-dir "$DESTINO" --shell /usr/sbin/nologin catrina
mkdir -p "$DESTINO"

echo "=== Archivos de Catrina ==="
cp "$ORIGEN/assistant.py" "$ORIGEN/requirements.txt" "$DESTINO/"

# claves: las que esten junto al codigo; si no, las que ya tiene la VM; si tampoco, se piden
clave() {   # archivo, pregunta, obligatoria (1/0)
  if [ -f "$ORIGEN/$1" ]; then cp "$ORIGEN/$1" "$DESTINO/"; return; fi
  if [ -s "$DESTINO/$1" ]; then return; fi
  local valor=""
  while :; do
    read -r -s -p "$2: " valor < /dev/tty
    echo
    if [ -n "$valor" ] || [ "$3" = 0 ]; then break; fi
  done
  if [ -n "$valor" ]; then printf '%s' "$valor" > "$DESTINO/$1"; fi
}
clave apikey.txt "Clave de Gemini (lo que dice apikey.txt en la laptop)" 1
clave apikey2.txt "Clave de Groq, gsk_... (apikey2.txt; Enter si no tienes)" 0
clave token.txt "Token del ESP32 (token.txt en la laptop, igual que TOKEN en secrets.h)" 1
# las respuestas guardadas de la VM crecen con el uso: solo se copian la primera vez
if [ ! -f "$DESTINO/respuestas.json" ] && [ -f "$ORIGEN/respuestas.json" ]; then
  cp "$ORIGEN/respuestas.json" "$DESTINO/"
fi

echo "=== Librerias de Python ==="
[ -d "$DESTINO/venv" ] || python3 -m venv "$DESTINO/venv"
"$DESTINO/venv/bin/pip" install -q --upgrade pip
"$DESTINO/venv/bin/pip" install -q -r "$DESTINO/requirements.txt"

chown -R catrina:catrina "$DESTINO"
chmod 600 "$DESTINO"/*.txt   # claves y contraseña: solo las lee el usuario catrina

echo "=== Modelos de Whisper (unos 600 MB, solo la primera vez) ==="
sudo -u catrina HOME="$DESTINO" "$DESTINO/venv/bin/python" -c \
  "from faster_whisper import WhisperModel; [WhisperModel(m, device='cpu', compute_type='int8') for m in ('base', 'small')]; print('Modelos listos')"

echo "=== Servicio ==="
cp "$ORIGEN/nube/catrina.service" /etc/systemd/system/catrina.service
systemctl daemon-reload
systemctl enable catrina
systemctl restart catrina
sleep 3
systemctl --no-pager --lines=0 status catrina || true
echo
echo "Listo. Para ver lo que hace:  sudo journalctl -u catrina -f"
