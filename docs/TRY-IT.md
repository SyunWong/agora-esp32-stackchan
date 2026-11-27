# Two milestones to record and share

Use your own deployment and robot. These steps create real sessions and may incur
provider usage. Start each milestone with the robot idle and a person present.

## 1. Your first AI conversation

1. Power the robot, wait for Wi-Fi/time synchronization and the idle face.
2. Tap the screen to start. Wait for the greeting.
3. Ask a short question, then a follow-up that depends on the first answer.
4. Listen for a complete response and check that the microphone still works
   while the speaker is active.
5. Tap to stop and confirm it returns to idle.

Success means that you heard the robot's response and it understood your speech.
A successful build, agent join or packet counter is not a substitute for this.

## 2. Call your robot from a browser

1. Open your deployment's HTTPS website in a supported browser.
2. Enter the name and shared password, then select **Call StackChan**. Grant
   microphone permission when prompted.
3. Have someone beside the robot accept the call. Start with touch acceptance;
   separately try the spoken Answer behavior.
4. Speak in both directions. Then briefly speak at the same time to check echo
   and whether either person's voice disappears.
5. Mute/unmute in the browser. Confirm that mute affects its microphone and
   that the displayed volume meters follow real audio.
6. Hang up. Verify that browser microphone capture stops and the robot leaves
   the human call. Check the expected idle/AI-resume state.

Use headphones on the browser side, or separate rooms, so the test does not
create an artificial acoustic loop between nearby devices.

## A useful short demo

Show the robot, one natural AI exchange, a browser calling it, and one human
exchange. End with the repository/build-guide link once it is published. Keep
account consoles, credentials and private conversations out of the recording.
Only show motion if that exact filmed build has been validated with motion.

## If something fails

| Symptom | First check |
| --- | --- |
| Stays on Wi-Fi screen | Network/password, 2.4 GHz compatibility and time synchronization |
| ERROR / TAP TO RETRY | Control connectivity and provider configuration; retry is not a battery diagnosis |
| Website says offline | Device control URL/key and Worker enablement; website health alone does not prove device registration |
| Connected but silent | Browser permission/output device, both audio directions and accepted call state |
| Echo or clipped speech | Room placement, speaker level and preserved AEC/reference configuration |
| Restarts when unplugged | Stop the demo and investigate power; do not bypass motor or power protection |

Never run simulated-device smoke tests against a Worker currently owning a real
robot: they can alter its session state.
