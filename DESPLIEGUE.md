# Subir Catrina a Google Cloud

La laptop ya no hace falta: el "cerebro" (`assistant.py`) corre en una máquina virtual
de Google Cloud y **el ESP32 se conecta a ella por internet** (puerto 5000, con la
contraseña de `token.txt`). El ESP32 solo necesita WiFi con internet.

```text
ESP32 (casa / ofrenda) --internet--> VM en Google Cloud (Querétaro)
   mic  ──── audio del micrófono ───>  Whisper (Catrina) + Groq (preguntas)
   ctl  <─── estado LED, "callar", "hablar"     Gemini / Groq (respuestas)
   spk  <─── voz de la respuesta  ────  Edge TTS
```

El código se baja del repositorio privado de GitHub. Las claves **no** están en
GitHub: la VM las pide una sola vez al instalar.

## Lo que necesitas

- Una cuenta de Google Cloud con facturación activa. Las cuentas nuevas traen
  US$300 de crédito por 90 días.
- La VM `e2-standard-2` (2 CPU, 8 GB) cuesta aprox. US$50 al mes si está prendida
  siempre (~US$1.7 al día). La IP fija agrega unos US$3 al mes.
- Un **token de GitHub de solo lectura** para clonar el repositorio privado:
  GitHub → foto → Settings → Developer settings → Personal access tokens →
  **Fine-grained tokens** → Generate new token. En *Repository access* elige solo
  este repositorio y en *Permissions → Contents* pon **Read-only**. Copia el token
  (`github_pat_...`): es la "contraseña" que pide `git clone`.
- A la mano, de la laptop: lo que dicen `apikey.txt`, `apikey2.txt` y `token.txt`.

Todo se hace en **Cloud Shell**, la terminal que trae la página de Google Cloud.
No hay que instalar nada en la laptop.

## Primera vez

1. **Abrir Cloud Shell.** Entra a <https://console.cloud.google.com>, elige tu
   proyecto y pulsa el botón `>_` (arriba a la derecha).

2. **Bajar el repositorio y crear la VM** (una sola vez). Como usuario pon
   `alexiscabrera296` y como contraseña pega el token de GitHub:

   ```bash
   git clone https://github.com/alexiscabrera296/catrina.git
   bash catrina/nube/crear_vm.sh
   ```

   Al final escribe la **IP fija**. Anótala.

   > Si dice que la zona o el tipo de máquina no existen, usa otra región:
   > `REGION=us-south1 bash catrina/nube/crear_vm.sh` y en los pasos siguientes
   > cambia `northamerica-south1-a` por `us-south1-a`.

3. **Entrar a la VM:**

   ```bash
   gcloud compute ssh catrina --zone=northamerica-south1-a
   ```

   La primera vez pregunta por una clave SSH: pulsa Enter en todo.

4. **Instalar Catrina** (dentro de la VM, unos 10 minutos):

   ```bash
   sudo apt-get install -y git
   git config --global credential.helper store   # recuerda el token de GitHub
   git clone https://github.com/alexiscabrera296/catrina.git
   sudo bash catrina/nube/instalar_vm.sh
   ```

   Te pide la clave de Gemini, la de Groq y el token del ESP32 (no se ven al
   pegarlas: es normal). Se quedan guardadas en la VM.

5. **Apuntar el ESP32 a la VM.** En la laptop, en `AgenteIA/esp32_assistant/secrets.h`
   (si no existe, copia `secrets.example.h` con ese nombre) cambia

   ```cpp
   const char* SERVER_HOST = "IP_DE_LA_VM";
   ```

   por la IP fija del paso 2. Revisa que `TOKEN` sea igual a `token.txt`. Sube el
   firmware desde el Arduino IDE.

6. **Comprobar.** Prende el ESP32 y en la VM:

   ```bash
   sudo journalctl -u catrina -f
   ```

   Debe aparecer `ESP32 conectado desde ...` y luego `Listo. Di "Catrina".`
   `Ctrl+C` para salir (Catrina sigue corriendo).

## Actualizar (cuando subas cambios a GitHub)

Desde Cloud Shell entra a la VM (paso 3) y:

```bash
cd catrina && git pull && sudo bash nube/instalar_vm.sh
```

Las claves y las respuestas guardadas de la VM no se pisan.

## Del día a día

Primero entra a la VM (paso 3).

| Quiero... | Comando |
|---|---|
| Ver qué hace | `sudo journalctl -u catrina -f` |
| Ver el log completo | `sudo tail -200 /opt/catrina/catrina.log` |
| Reiniciarla | `sudo systemctl restart catrina` |
| Cambiar una clave | `sudo nano /opt/catrina/apikey.txt` y luego reiniciarla |

Desde Cloud Shell (fuera de la VM):

| Quiero... | Comando |
|---|---|
| Apagar la VM (deja de cobrar CPU) | `gcloud compute instances stop catrina --zone=northamerica-south1-a` |
| Prenderla otra vez | `gcloud compute instances start catrina --zone=northamerica-south1-a` |

Al prender la VM, Catrina arranca sola. Si se cierra o se traba, se reinicia sola
en 5 segundos (servicio `catrina` de systemd).

## Si algo falla

- **`git clone` dice "Authentication failed":** el token de GitHub no tiene acceso
  a este repositorio o venció; crea otro (ver "Lo que necesitas").
- **La tira LED parpadea en rojo y el log no dice "ESP32 conectado":** revisa
  `SERVER_HOST` en `secrets.h` y que la VM esté prendida. El ESP32 lo reintenta cada 3 s.
- **El log dice "conexion rechazada ... no es el ESP32":** el `TOKEN` de `secrets.h`
  no es igual al de la VM (`sudo cat /opt/catrina/token.txt`). También aparece si
  alguien de internet prueba el puerto: es normal y no puede hacer nada sin la contraseña.
- **El log dice `(ESP32) arranque: voltaje bajo`:** la fuente del ESP32 no aguanta;
  usa un cargador de 5 V y 2 A.
- **Se oye entrecortado:** la conexión de internet del lugar no da 32 KB/s de subida
  y bajada estables; acerca el router o usa otra red.

## Usarla otra vez desde la laptop

`AgenteIA/iniciar_catrina.bat` sigue funcionando: pon en `SERVER_HOST` la IP de la laptop en
tu red (`ipconfig`) y, cuando Windows pregunte, permite a Python en redes privadas.
