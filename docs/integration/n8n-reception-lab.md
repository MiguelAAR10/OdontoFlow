# Laboratorio de recepción ODONTO SMART con n8n

Este laboratorio usa `provider=test`: persiste conversaciones, mensajes y
acciones en OdontoFlow, pero nunca intenta enviar un mensaje a WhatsApp. La
respuesta visible se devuelve desde el webhook/chat de n8n.

## 1. Preparar PostgreSQL y el laboratorio

En PowerShell, desde la raíz del backend:

```powershell
docker compose up -d db
docker exec odontoflow-backend-security-db-1 createdb -U odontoflow odontoflow_n8n_lab
$env:DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow_n8n_lab'
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe scripts\bootstrap_n8n_lab.py
```

`createdb` se ejecuta una sola vez; si informa que la base ya existe, se
continúa con Alembic. El último comando carga el catálogo sintético, crea el canal
`odonto-smart-lab` y rota tres credenciales de mínimo privilegio. Los tokens se
guardan en `.env.n8n.local`, que Git ignora; no se imprimen ni se guardan en
PostgreSQL en texto plano.

## 2. Levantar la API

```powershell
$env:DATABASE_URL='postgresql+psycopg://odontoflow:odontoflow@127.0.0.1:5434/odontoflow_n8n_lab'
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Comprobar `GET http://127.0.0.1:8000/health`. n8n Cloud necesitará una URL HTTPS
pública hacia esta API; no debe exponerse PostgreSQL ni reutilizarse el token de
un perfil para otra responsabilidad.

## 3. Variables de n8n

Copiar desde `.env.n8n.local` hacia credenciales/variables privadas de n8n:

- `ODONTOFLOW_INBOUND_TOKEN`: solo ingresa mensajes.
- `ODONTOFLOW_AGENT_TOKEN`: solo consulta y ejecuta herramientas de recepción.
- `ODONTOFLOW_OPERATOR_TOKEN`: solo reanuda una conversación entregada a humano.
- `ODONTOFLOW_CHANNEL_ACCOUNT_ID`: referencia informativa del canal sintético.

No subir el archivo de secretos a n8n como dato de workflow ni pegar tokens en
un nodo Code.

## 4. Primer recorrido determinista

1. El Webhook/Form Trigger de n8n recibe el texto de prueba.
2. Un HTTP Request ingresa el mensaje con el token inbound:

   `POST /internal/messages/inbound`

   ```json
   {
     "schema_version": "1.0",
     "provider": "test",
     "channel_account_external_id": "odonto-smart-lab",
     "provider_message_id": "{{$json.message_id}}",
     "external_contact_id": "{{$json.contact_id}}",
     "phone_e164": "+51999000001",
     "message_type": "text",
     "text": "{{$json.text}}",
     "occurred_at": "{{$now.toISO()}}"
   }
   ```

3. Guardar `conversation_id` y `message_id` de la respuesta.
4. El agente decide; las lecturas usan `tool_version=1.0` e
   `idempotency_key=null`. Las mutaciones usan `tool_version=1.1` y un UUIDv4.
5. Un único broker HTTP llama `POST /agent-tools/call` con el token agent. Sus
   headers `X-Request-Id` y `X-Correlation-Id` deben ser idénticos a los UUID
   enviados en el cuerpo.
6. n8n presenta la respuesta en el chat de simulación. Si se desea auditarla,
   también se persiste con `POST /internal/conversations/{conversation_id}/outbound`.

El broker solo puede usar los nombres publicados en OpenAPI. En particular,
cancelar requiere `propose_cancellation` y luego `confirm_cancellation` desde
otro mensaje inbound. `resume_automation` no es una herramienta del LLM: un
operador usa `POST /internal/conversations/{conversation_id}/resume`.

## 5. Límites del laboratorio

- Gemini o ChatGPT pueden cambiarse sin cambiar el backend: el modelo decide;
  OdontoFlow valida permisos, estado, contacto, disponibilidad e idempotencia.
- Durante `human_handoff`, todas las herramientas del agente quedan bloqueadas.
- Los outbounds del proveedor `test` no pueden ser reclamados por un dispatcher
  externo.
- Telegram y WhatsApp quedan fuera hasta que los casos de aceptación del canal
  sintético pasen.
