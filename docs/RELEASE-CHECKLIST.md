# Publication and reproduction checks

This file distinguishes evidence from release work. It is not a claim that a
new developer has completed the recipe.

## Existing project evidence

- Real local AI conversation and local two-way human audio have been observed.
- The owner reported clear two-way audio in a real cloud/browser call.
- A separate scripted trial confirmed incoming lift, spoken Answer and return
  to idle, but had no real browser RTC peer. Do not combine these into a claim
  that one continuous filmed end-to-end trial verified everything.
- The updated caller frontend passed offline logic checks and cloud asset/API
  checks. A new real browser walkthrough of that exact UI still needs recording.
- Head motion is currently disabled for power investigation. The current device
  has volume 90; the public source recipe deliberately starts at the earlier 55.
- No five-minute human-active trace or long-duration stability acceptance is
  claimed. Battery percentage is approximate.

## Before a public “follow this tutorial” release

- [ ] Choose the public repository URL and license for original project code.
- [ ] Confirm redistribution rights for the supplied caller design and images;
      retain vendor licenses and attribution. An enhanced image is not an
      authoritative hardware reference photograph.
- [ ] Recreate the package from its explicit source allowlist and inspect its
      manifest; do not publish the experiment repository or its history wholesale.
- [ ] Run the documented source preparation on a clean machine with the pinned
      ESP-IDF and Board Manager versions; record the resolved dependency lock.
- [ ] Validate first-time Worker creation, domain routing and Secrets setup under
      a fresh account/project. Existing live upgrades do not prove fresh setup.
- [x] Provide portable cloud creation and backup/partition-check/app-only tools
      with offline failure-injection tests and explicit apply modes.
- [ ] Run the new installer on a second supported StackChan. The owner-specific
      installer is not exported; its identity and backup hashes remain private.
- [ ] Complete both listening milestones using the exported source and new setup.
- [ ] Test wrong password, cancel, decline, mute, hangup, browser refresh and
      reconnection; record firmware/release identifiers with results.
- [ ] Check sustained human calling and repeated sessions for the duration
      advertised. Do not publish unmeasured latency or battery-life numbers.
- [ ] Add the author's demo video and a working public source link.

The source package can be reviewed now. Physical validation of the new installer and clean-account
cloud deployment remain release gates; the current guide marks them explicitly.
