# Infer Take-Home Assignment

Build a small web app that lets a user pull their policy documents from a personal lines carrier portal.

## Flow

1. User opens the UI, picks a carrier from a dropdown (like Progressive, Geico, Allstate etc.)
2. User enters their portal username and password
3. App kicks off the login on the backend
4. When the carrier prompts for MFA, the UI surfaces an input field. User types in the code they received on their phone or email and submits
5. App pulls the policy documents and renders them in the UI.

## Two layers

- **Frontend:** dropdown, credential fields, MFA prompt that appears when needed, document viewer at the end. Keep it ugly, we care about it working
- **Backend API:** handles the actual portal automation, session management, document fetch. Should respond fast enough that the full flow from MFA submission to document render is under 8 seconds.

## What we're evaluating

- Working code we can run locally with a short README
- Latency from MFA submission to document on screen
- Reliability and session reuse if the user runs more than once.

## Notes

- You'll need real login credentials to test against. Part of the task is figuring out how to get them, whether that's a friend, family member, or someone in your network with a policy. This is the same problem you'd hit on day one at Infer, so we want to see how you handle it. You can choose whichever carrier portal makes sense for you.
- Stack is your call. Use what you're fastest in.
- Deadline: 72 hours from the day of sharing this.

## Deliverable

Send the repo link and a 2-3 minute Loom walking through the code and a live run with a real policy.
