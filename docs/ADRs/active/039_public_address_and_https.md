# ADR-039: The public address and HTTPS

**Status:** Accepted
**Date:** 2026-11-16
**Deciders:** Simon (PRIO), Claude Code
**Informed:** whoever sets up a new server for this service

---

## Context

Partners reach the API at one address, `https://crafdapi.viewsforecasting.org`. Until 2026-11-16 no
record said how that address is made to work. When the service moved to a new server, the steps
were done from memory, in three places, by two people. If the server is replaced again, nothing
here would say what to do.

## Decision

1. **One address.** The service is reached only at `https://crafdapi.viewsforecasting.org`. Partners
   are never given the server's IP address or a plain `http://` address.
2. **The domain points at the server.** An `A` record for `crafdapi.viewsforecasting.org` holds the
   server's IP address. The record is changed by the administrator of `viewsforecasting.org`, not by
   this repository's maintainer. Moving to a new server means asking them to change it.
3. **nginx sits in front of the service.** The service listens only on `127.0.0.1:8001`. nginx
   receives requests on the public ports (80 and 443) and passes them to it. The nginx site file is
   `/etc/nginx/sites-enabled/crafdapi`.
4. **The certificate comes from Let's Encrypt, through certbot.** It is created once per server with
   `sudo certbot --nginx -d crafdapi.viewsforecasting.org`, after the domain points at the server
   (certbot fails before that). certbot also installs its own scheduled renewal. Certificates last
   90 days; nobody renews them by hand.

## Consequences

- Setting up a new server has three steps outside the code: the domain record, the nginx site, and
  certbot. They are written down, in that order, in the release runbook
  (`deployment/RELEASE_RUNBOOK.md`).
- Changing the domain, the proxy (for example to Caddy, which handles certificates itself) or the
  certificate provider is a change to this ADR.

## Record

- 2026-11-16: the domain was moved to the new server (159.69.0.79), and certbot was run on it.
  Checked that day: `https://crafdapi.viewsforecasting.org/ping` answered 200, and the certificate
  was valid until 2027-02-14.
