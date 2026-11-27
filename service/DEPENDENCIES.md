# PoC dependencies

Agora Web SDK 4.24.10, downloaded from https://registry.npmjs.org/agora-rtc-sdk-ng/-/agora-rtc-sdk-ng-4.24.10.tgz. Local served file is ignored; SHA256: 31eef1a11301aea58e4df3e584ff16b285f3dc2585c21fca75b75d6583ca0f0b.

`agora_token/` files are the official AgoraIO/Tools Python3 AccessToken2 builder, fetched 2026-11-26 from https://github.com/AgoraIO/Tools/tree/master/DynamicKey/AgoraDynamicKey/python3/src . Source copyright and repository LICENSE retained. These files require only Python standard library.

`server.py` binds only 127.0.0.1:8765 and rejects foreign Host/Origin/cross-site requests. It is a temporary single-session PoC, not a deployable cloud authentication service. It never starts an Agent. Existing App Certificate remains server-side; device receives only a 30-minute WHIP JWT, and browser gets RTC credentials valid for at most 10 minutes.
