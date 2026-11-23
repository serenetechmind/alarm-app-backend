# Deploying the Alarm API (Render + Firestore)

## 1. Turn on Firestore (once)
Firebase Console -> your project -> Build -> **Firestore Database** -> Create database ->
**Production mode** -> pick a region close to your users (cannot be changed later).
Leave the default rules (deny all): the server uses the Admin SDK, which is not affected by them,
and this stops anyone reading your data directly.

## 2. Render
1. Put this folder in a GitHub repo (private is best).
2. render.com -> New -> Web Service -> pick the repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
3. Environment -> add `FIREBASE_CREDENTIALS_JSON` and paste the ENTIRE contents of your
   Firebase service-account JSON (Firebase Console -> Project settings -> Service accounts ->
   Generate new private key). Never commit that file to GitHub.
4. Deploy. Open `https://<your-service>.onrender.com/` : it should show
   `"fcm": "live"` and `"database": "firestore"`.

## 3. After switching from SQLite
The old data is not carried over. Phones register again by themselves (the app handles it and shows
a new pair code), and developer API keys must be created again.

## Notes
- Free Render instances sleep after ~15 minutes without traffic, and the first request then takes
  up to a minute. Your data is safe in Firestore, but a sleeping server delays alarms created by
  other apps. A free uptime monitor pinging `/` every 5 minutes keeps it awake, or use a paid instance.
- API keys and device tokens are stored only as hashes. A lost API key cannot be recovered; make a new one.
