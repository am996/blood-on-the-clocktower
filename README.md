# Blood on the Clocktower — Virtual Grimoire

Run the app with Python 3.9+:

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

Open `http://localhost:5000` on the Storyteller's device, create a room, then open the displayed `/room/ABCD` URL on other devices on the same network (using the host computer's LAN IP rather than `localhost`).

If port 5000 is already in use, choose another port:

```bash
PORT=5050 python3 app.py
```

Then open `http://localhost:5050`.

This is an in-memory prototype: rooms reset when the process restarts. For deployment, set a strong `SECRET_KEY`, restrict CORS, add authentication for the Storyteller, and move `rooms` to a shared persistent store such as Redis/Postgres.
