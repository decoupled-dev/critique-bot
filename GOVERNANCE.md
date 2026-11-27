# Governance

crit starts with a small, simple model and will change it as the community grows.

## Roles

- **Maintainer(s)** <!-- TODO before launch: name the maintainer(s) and their GitHub handles. -->
  set the direction (the staged plan in [`plan/`](plan/README.md)), review and merge pull
  requests, cut releases, and handle security reports ([`SECURITY.md`](SECURITY.md)).
  While there is a single maintainer, that person decides; with several, decisions are by
  rough consensus among them, and the lead maintainer breaks ties.
- **Contributors** are everyone who opens an issue, a discussion or a pull request.
  Contributors who keep landing good reviews and changes in an area are invited to become
  maintainers.

## How decisions are made

- Everyday changes: a pull request with green CI and one maintainer's approval.
- Changes to the harness (prompts, tools, the loop, parsing) also need a before/after
  `crit eval` comparison on a fixed model ([`CONTRIBUTING.md`](CONTRIBUTING.md)).
- Larger changes (a new layer, a new base dependency, a change to the security model or to a
  public interface such as the server API) start as an issue or a short design note; the
  outcome is recorded with its reason in [`plan/decisions.md`](plan/decisions.md).
- Security fixes may be developed in private and merged without the usual public review.

## Releases

Semantic versioning. Releases are tagged by a maintainer and built, signed and published by
CI (`.github/workflows/build.yml`), never from a laptop.

## Code of conduct

Everyone in the project's spaces follows the [Code of Conduct](CODE_OF_CONDUCT.md).
