# Alarm API (FastAPI)

## Run locally
```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```
Open http://127.0.0.1:8000/docs for interactive docs (you can test every endpoint there).

Without Firebase credentials, pushes are only printed in the terminal ("DRY RUN").
For real pushes: Firebase Console > Project settings > Service accounts > Generate new private key, then
```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/serviceAccount.json
```

## Try the full flow (curl)
```bash
# 1. Phone registers (the Android app will do this)
curl -X POST localhost:8000/v1/device/register -H "Content-Type: application/json" \
  -d '{"fcm_token":"test-token","timezone":"Africa/Lagos"}'
# -> device_token, pair_code

# 2. A developer signs up
curl -X POST localhost:8000/v1/developers -H "Content-Type: application/json" \
  -d '{"app_name":"My Todo App"}'
# -> api_key

# 3. The developer connects using the code the user typed in
curl -X POST localhost:8000/v1/connections -H "Authorization: Bearer API_KEY" \
  -H "Content-Type: application/json" -d '{"pair_code":"123456"}'
# -> connection_id

# 4. Create an alarm (Mon-Fri 07:30)
curl -X POST localhost:8000/v1/alarms -H "Authorization: Bearer API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"connection_id":1,"time":"07:30","repeat_days":[2,3,4,5,6],"label":"Gym"}'
```

## Deploy
Needs a host with persistent disk for SQLite (Railway, Render, a VPS). Netlify can't run this.
Later: swap SQLite for Postgres/Firestore.
