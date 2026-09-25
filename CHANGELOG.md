# Changelog

## [1.4.0](https://github.com/jeanclode-hq/jeanclode/compare/v1.3.2...v1.4.0) (2026-09-25)


### Features

* let triage choose the fixer's LLM credential and model tier ([#44](https://github.com/jeanclode-hq/jeanclode/issues/44)) ([3a2291a](https://github.com/jeanclode-hq/jeanclode/commit/3a2291a886818ea10feef8a1a8b7359208cd5edb))


### Bug Fixes

* **backend:** only fix/resolve runs drive an issue's status ([#45](https://github.com/jeanclode-hq/jeanclode/issues/45)) ([8f14a9a](https://github.com/jeanclode-hq/jeanclode/commit/8f14a9a9f43ce814be3b308d2c2b056e9f1282da))

## [1.3.2](https://github.com/jeanclode-hq/jeanclode/compare/v1.3.1...v1.3.2) (2026-09-24)


### Bug Fixes

* **backend:** give the multi-credential migration its own revision id ([#40](https://github.com/jeanclode-hq/jeanclode/issues/40)) ([74c7b5b](https://github.com/jeanclode-hq/jeanclode/commit/74c7b5bddc23d16af5018bfd988398ac2c9a40ee))

## [1.3.1](https://github.com/jeanclode-hq/jeanclode/compare/v1.3.0...v1.3.1) (2026-09-24)


### Bug Fixes

* **backend:** build the image from uv.lock instead of floating deps ([#38](https://github.com/jeanclode-hq/jeanclode/issues/38)) ([2018a30](https://github.com/jeanclode-hq/jeanclode/commit/2018a30bd49d9073cf3d2473c31545d9aa558b71))

## [1.3.0](https://github.com/jeanclode-hq/jeanclode/compare/v1.2.0...v1.3.0) (2026-09-24)


### Features

* **cli:** log what each agent session loaded, raise fixer max turns to 250 ([#37](https://github.com/jeanclode-hq/jeanclode/issues/37)) ([991c55e](https://github.com/jeanclode-hq/jeanclode/commit/991c55ed67bef103b244b9067776eb49abad8f01))
* several auths per skill or MCP server, plus a host-only "no auth" type ([#36](https://github.com/jeanclode-hq/jeanclode/issues/36)) ([2f07777](https://github.com/jeanclode-hq/jeanclode/commit/2f077770c2e1e7d94b70d2b52d30ed0fa6c0ce74))


### Bug Fixes

* **backend:** exclude GitLab bot accounts from top users ([#34](https://github.com/jeanclode-hq/jeanclode/issues/34)) ([1b88e95](https://github.com/jeanclode-hq/jeanclode/commit/1b88e950149c2210772b55b60b1b3fce0b96addd))

## [1.2.0](https://github.com/jeanclode-hq/jeanclode/compare/v1.1.4...v1.2.0) (2026-09-24)


### Features

* add better stats and ui and a guide ([a121b9a](https://github.com/jeanclode-hq/jeanclode/commit/a121b9afe41724eff1bd83914c7dbf24b16b99a1))

## [1.1.4](https://github.com/jeanclode-hq/jeanclode/compare/v1.1.3...v1.1.4) (2026-09-17)


### Bug Fixes

* **respond:** clone related repos for cross-repo context, tighten no-PR guardrail ([#28](https://github.com/jeanclode-hq/jeanclode/issues/28)) ([256abb6](https://github.com/jeanclode-hq/jeanclode/commit/256abb6e1030078f1e85bd024436bbab6448ffa5))
* **security-proxy,cli:** stream git clone responses, retry transient clone failures ([#30](https://github.com/jeanclode-hq/jeanclode/issues/30)) ([af01f11](https://github.com/jeanclode-hq/jeanclode/commit/af01f11f509c60e959548908e681fbc16790393c))

## [1.1.3](https://github.com/jeanclode-hq/jeanclode/compare/v1.1.2...v1.1.3) (2026-09-16)


### Bug Fixes

* **dashboard:** order and date the issues list by first_seen ([#26](https://github.com/jeanclode-hq/jeanclode/issues/26)) ([aa047bf](https://github.com/jeanclode-hq/jeanclode/commit/aa047bf78c6af440a0a4113ca651a91a43eb7811))

## [1.1.2](https://github.com/jeanclode-hq/jeanclode/compare/v1.1.1...v1.1.2) (2026-09-16)


### Bug Fixes

* **backend:** dispatch workflows on labels set when a PR/issue is created ([#24](https://github.com/jeanclode-hq/jeanclode/issues/24)) ([cad4089](https://github.com/jeanclode-hq/jeanclode/commit/cad4089d56f33c13e10f6dc97cf8b1436ea8b09a))

## [1.1.1](https://github.com/jeanclode-hq/jeanclode/compare/v1.1.0...v1.1.1) (2026-09-14)


### Bug Fixes

* **backend:** scope Sentry dispatch gate and concurrency perimeter to the connected root org ([7d426f6](https://github.com/jeanclode-hq/jeanclode/commit/7d426f6306854f0cf51d1b37caa80737b35c7539))

## [1.1.0](https://github.com/jeanclode-hq/jeanclode/compare/v1.0.1...v1.1.0) (2026-09-14)


### Features

* **cli:** per-file changes dropdown in PR summary, noise files kept out of agent diffs ([#21](https://github.com/jeanclode-hq/jeanclode/issues/21)) ([adbebe2](https://github.com/jeanclode-hq/jeanclode/commit/adbebe2e0020b2773ea1bcbb0154725facef9e8e))

## [1.0.1](https://github.com/jeanclode-hq/jeanclode/compare/v1.0.0...v1.0.1) (2026-09-13)


### Bug Fixes

* **frontend:** raise integration cards and brighten placeholders ([2b98a91](https://github.com/jeanclode-hq/jeanclode/commit/2b98a919fb6f796595b32135b2bcc24988a367e5))

## 1.0.0 (2026-09-14)

First public release.
