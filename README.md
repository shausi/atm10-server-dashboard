# ATM10 Server Dashboard

A small local web panel for running an **All the Mods 10** (Minecraft 1.21.1, NeoForge) server on your own Windows PC.
It starts and stops the server, shows the live console, manages players, files, mods and backups, and can check
whether a newer ATM10 release is out.

It is a personal, self-hosted tool. It is not a public service and it does not host or share anything.

> Not affiliated with CurseForge, Overwolf, the All the Mods team, Mojang or Modrinth.

## Features

- **Overview:** players, TPS, CPU, memory, disk, uptime, recent console.
- **Start, stop, restart, force kill.** The dashboard starts the server itself, so it can send commands to it.
- **Console:** live output and a command box.
- **Players:** whitelist, operators, bans, kick. Works while the server is stopped too.
- **Files:** browse and edit server files. Deleted items go to a trash folder.
- **Mods:** list installed mods, search Modrinth (NeoForge 1.21.1, server-compatible), install with required
  dependencies, spot mods that are already installed, and build a zip so friends can install the same extra mods.
- **Backups:** create (also while the server runs), restore, download, delete.
- **Schedules:** automatic backups, restarts, announcements and commands.
- **Updates:** compares your server with the newest ATM10 release and updates from the official server pack,
  keeping your world, whitelist, ops, settings and the mods you added. Also updates from a zip you download yourself.
- **Configuration:** edit `server.properties` and server memory.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer (standard library only, nothing to install)
- Java 21 and an ATM10 server folder that contains `startserver.bat` from the official server pack

## Run it

1. Put your ATM10 server in `D:\MinecraftServers\ATM10`, or set these environment variables first:
   - `ATM10_BASE`: the folder that holds the server, backups and data (default `D:\MinecraftServers`)
   - `ATM10_ROOT`: the server folder itself (default `ATM10_BASE\ATM10`)
2. Run `python server.py`, or double-click `Start Dashboard.vbs` to run it with no console window.
3. Open `http://localhost:7870/`.

## Security

- It listens on `127.0.0.1` only. Nothing is reachable from the network.
- Every request needs a random token that is created at startup and only given to the page it serves.
  Requests with another `Host` or `Origin` are refused, so other websites cannot control it.
- File access is limited to the server folder.
- Do not forward port 7870 on your router.

## CurseForge API (optional)

The update check uses the CurseForge Core API, and only if you add your own API key on the **Updates** page.

- It reads the file list of one modpack, All the Mods 10, and downloads that modpack's official server pack to your own PC.
- The key is stored only on your PC and is never sent anywhere except to CurseForge.
- It does not store or cache data returned by the API. The result of each check is shown on screen and then dropped.
- It does not re-host, redistribute or share CurseForge files.

You need your own approved key. See CurseForge's article on how to apply. Without a key everything else works,
and you can update from a server pack zip you download yourself.

## Modrinth

Mod search and install use the public Modrinth API with a descriptive `User-Agent`. Files are checked against the
hash Modrinth publishes before they are used.

## Limits

- Windows only.
- A server started outside the dashboard shows as "Running outside the dashboard" and cannot be controlled.
- Mod compatibility is filtered by loader and Minecraft version only. A mod can still clash with the pack's own
  mod versions, so make a backup before adding mods.
- Schedules only run while the dashboard is running.

## License

MIT. See `LICENSE`.
