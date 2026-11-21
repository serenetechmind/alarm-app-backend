# Deploying the Alarm API (Render example)

1. Put this folder in a GitHub repo (private is best).
2. render.com -> New -> Web Service -> pick the repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
3. Environment -> add `FIREBASE_CREDENTIALS_JSON` and paste the ENTIRE contents of your
   Firebase service-account JSON file (Firebase Console -> Project settings -> Service accounts
   -> Generate new private key). Never commit that file to GitHub.
4. Deploy. Open `https://<your-service>.onrender.com/` : it should say `"fcm": "live"`.
5. In the Android app: Connected apps -> Server URL -> paste that https address -> Save.

## Important: storage
This version keeps data in SQLite (`alarmapi.db`). On free hosting the disk is wiped on every
restart, which forgets all phones and alarms (the app re-registers itself and shows a new pair code).
For real use, attach a persistent disk (set `DB_PATH` to a path on it) or move the data to
Firestore/Postgres: a good next step.
