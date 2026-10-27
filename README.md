# Catrina (servidor para Google Cloud)

El "cerebro" del asistente de voz Catrina: corre en una VM de Google Cloud y el
ESP32 se conecta a ella por internet (puerto 5000, con la contraseña de `token.txt`).

| Archivo | Qué es |
|---|---|
| `assistant.py` | El servidor: reconocimiento de voz, IA y voz |
| `requirements.txt` | Librerías de Python |
| `respuestas.json` | Respuestas guardadas (se copian a la VM solo la primera vez) |
| `nube/` | Scripts para crear la VM e instalar el servicio `catrina` |
| `DESPLIEGUE.md` | Paso a paso para ponerla en Google Cloud |

El firmware del ESP32 está en la carpeta `AgenteIA` de la laptop. Las claves
(`apikey.txt`, `apikey2.txt`, `token.txt`) no están en el repositorio: la VM las
pide al instalar.
