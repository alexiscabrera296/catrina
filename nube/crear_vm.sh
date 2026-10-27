#!/bin/bash
# Crea la VM de Catrina, su IP fija y el permiso del puerto 5000 en el firewall.
# Se corre una sola vez en Cloud Shell (el boton >_ arriba a la derecha de
# console.cloud.google.com). Para cambiar algo:  MAQUINA=e2-standard-4 bash nube/crear_vm.sh
set -euo pipefail

REGION=${REGION:-northamerica-south1}   # Queretaro, la mas cercana a Mexico
ZONE=${ZONE:-${REGION}-a}
MAQUINA=${MAQUINA:-e2-standard-2}       # 2 CPU, 8 GB: alcanza porque Groq transcribe las preguntas
NOMBRE=${NOMBRE:-catrina}

gcloud services enable compute.googleapis.com

# IP fija: el ESP32 la tiene escrita, asi que no debe cambiar si la VM se reinicia
gcloud compute addresses describe catrina-ip --region="$REGION" >/dev/null 2>&1 || \
  gcloud compute addresses create catrina-ip --region="$REGION"
IP=$(gcloud compute addresses describe catrina-ip --region="$REGION" --format='value(address)')

# el ESP32 entra por el puerto 5000 (lo protege la contraseña de token.txt)
gcloud compute firewall-rules describe catrina-esp32 >/dev/null 2>&1 || \
  gcloud compute firewall-rules create catrina-esp32 --allow=tcp:5000 \
    --target-tags=catrina --description="ESP32 de Catrina"

gcloud compute instances describe "$NOMBRE" --zone="$ZONE" >/dev/null 2>&1 || \
  gcloud compute instances create "$NOMBRE" --zone="$ZONE" --machine-type="$MAQUINA" \
    --image-family=debian-12 --image-project=debian-cloud --boot-disk-size=20GB \
    --address="$IP" --tags=catrina

echo
echo "VM lista. IP fija: $IP"
echo "Ponla en SERVER_HOST de esp32_assistant/secrets.h y sube el firmware al ESP32."
