# Chris: to-do when home (about 15 minutes)
1. **Render**: sign up at render.com and add your card. The plan is Starter at about $7/mo, plus a disk at about $0.25/mo.
2. **Render API key**: Account Settings → API Keys → Create. Paste it into the secure box Littledude sends you, never into chat.
3. **GitHub**: let Littledude put the code in a private repo `amex-submission`. Then in Render choose "Connect GitHub" and allow that one repo. (If you don't have a GitHub account, make a free one.)
4. **Record email** (so Submit emails littledude@mail.grokbot.com automatically):
   GoDaddy → Email & Office Dashboard → littledude@coxresearch.com → Manage → Advanced Settings → turn **SMTP Authentication ON**.
   Heads-up: GoDaddy says that if "security defaults" are on, this switches them off for the whole organization. If littledude uses MFA, make an app password and use that instead.
   Then in Render → amex-submission → Environment, type the littledude mailbox password into **SMTP_PASSWORD**.
5. **Decide** whether every record email should also CC chris@coxresearch.com (yes or no).
6. **GoDaddy DNS** (after Littledude creates the service): sign in to GoDaddy. Littledude adds one record, CNAME `amex` → `amex-submission.onrender.com`. Your email is not touched.
7. **Say go** for the TEST email to chris@coxresearch.com only. Nothing goes to employees until you approve.
