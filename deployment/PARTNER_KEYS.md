# Partner keys: create, save, send, revoke

*Internal. For the maintainer who gives a partner organisation its key. Partners read the partner
page (`docs/api/README.md`) instead.*

Every partner organisation gets its own key. Every key sees the same data. Separate keys exist for
one reason only: so that one partner can be switched off without touching the others.

If partners ever need to receive different data, that will be a new delivery service, with its own
API and its own Appwrite project. It will not be this one: this service is fixed to serving the
same data to every key.

This repository is public. **Never write a key, any part of a key, or which organisations hold keys
into it.**

## 1. Create the key (Appwrite console, ~2 min)

1. Log in to the Appwrite console and open CRAF'd's project.
2. *Overview → API keys → Create API key.*
3. **Name:** the organisation plus the month it was issued, for example `partner-ocha-2026-11`.
4. **Expiration:** one year.
5. **Scopes:** read-only, exactly these: `buckets.read`, `files.read`, `rows.read`.

## 2. Save it (Bitwarden)

Save the key in your Bitwarden vault, under the key's name. (If you don't have a Bitwarden vault,
get help setting one up before you create the key: simmaa@prio.org.) In the entry's notes, write the
organisation, the contact person, the date created and the expiry date. These entries are the record
of which partner holds which key.

(The VIEWS platform should have one dedicated Bitwarden vault for these keys. It does not yet.)

## 3. Send it (Bitwarden Send and email)

1. Create a Bitwarden Send containing the key, and copy its link.
2. Email the link to the partner's contact from your institutional email address (PRIO, Uppsala, or
   wherever you work). **Always CC views-platform@prio.org.**
3. In the same email, write:
   - the address of the API: `https://crafdapi.viewsforecasting.org`;
   - how to use the key: "send your key in a request header named `X-API-Key`";
   - a link to the partner page in this repo, `docs/api/README.md`, which explains the rest;
   - the expiry date.

   Never write the key itself in the email; it travels only in the Bitwarden Send.

What the partner does with the key after that is theirs to manage.

## 4. Revoke a key (~1 min)

1. In the Appwrite console, open CRAF'd's project and delete the key by its full name. Its next
   request is refused. No restart is needed.
2. Write the date it was revoked in the key's Bitwarden entry.

## 5. Renew a key (before it expires)

1. One month before the old key expires, create, save and send a new key (steps 1-3).
2. In the Appwrite console, rename the old key by putting `RETIRED-` in front of its name, for
   example `RETIRED-partner-ocha-2026-11`. It keeps working until its expiry date, so the partner has
   a month to switch.
3. Do nothing else: the old key stops working on its own when it expires.

**A leaked key:** delete it at once (step 4), then create, save and send a new one.
