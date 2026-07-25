# Security

## Reporting a vulnerability

Open a [private security advisory](https://github.com/jmdevita/openxray-generator/security/advisories/new)
on this repository. That channel is not public, so a report stays between us
until there is something to publish.

Please do not open a public issue for a vulnerability.

There is no bounty and no SLA. This is a spare-time project; a real report
will get a real answer, but not necessarily a fast one.

## What is in scope

The Generator is a **LAN service** that holds credentials: your media-server
token, your TMDb key, and optionally your AudD token. Anything that leaks
those, or that lets someone who can reach the dashboard act without the web
token, is in scope. So is anything in the timeline files themselves, since
those are designed to be shared with strangers.

The companion Hub is a separate service and a separate report; if you are
unsure which one you are looking at, say so and we will sort it out.

## What is already known and accepted

- **Port 8080 is bound on all interfaces** by the shipped compose file,
  because reaching the dashboard from another machine on your LAN is the
  normal way to use it. Note that Docker publishes ports by writing NAT
  rules, which **bypass a host firewall like ufw**: firewalling the host does
  *not* close this. Put it behind an authenticating reverse proxy, or set
  `AUTH_METHOD=external`, before it can reach anything untrusted.
- **The web token is printed to the container logs** on first boot. Anyone who
  can read your Docker logs can sign in. Pin your own with `XRAY_WEB_TOKEN`.
- **Third-party data is fetched and rendered.** TMDb, Wikidata and Wikipedia
  responses become cast names, trivia and titles. They are treated as
  untrusted text, but the trust boundary is worth knowing about.
- **Timelines are not signed.** A timeline you import is only as trustworthy
  as where you got it, which is why a hub gates uploads behind review rather
  than publishing them on arrival.
