# Native mobile client study: Swift, Flutter, or stay PWA-only (#672)

Paper study, iOS only. No product code, prototype or repo layout is proposed. Android is out of scope, and the PWA
(Stables) stays as the phone client in the meantime (owner decision, 2026-10-22, option B on #672). The study exists so
the owner can settle the direction of #31 (install and verify Xcode prerequisites for native iOS client work), whose
standing assumption is native Swift.

## How to read the claims

Each claim carries one of three marks:

- **Repo**: read in this repository at the file named. Verified here.
- **Web**: taken from the linked page, found by search on 2026-10-22. The page was read as a search summary, not
  tested on a device.
- **Unverified**: from general knowledge or reported anecdotally, not checked in this study. Treat it as a question to
  settle on a real iPhone before relying on it.

Nothing here was run on a device. The owner's iPhone, the MacBook and Xcode were not used.

## Today's baseline

| Area | What exists | Source |
| --- | --- | --- |
| Client | Stables, an installable PWA in its own repository (`dflippojr/stables`), pinned at `v0.1.0` and served by the Server over Tailscale HTTPS. It is a client of `/api/v1` and `/api/admin/v1` only. | Repo: `docs/web.md`, `tools/stables/pin.json` |
| Notifications | A self-hosted ntfy server. Approvals carry Approve/Deny action buttons that the ntfy iOS app sends straight to the daemon, so the phone must be on the tailnet. The buttons carry a per-approval secret instead of a login. | Repo: `harness_modules/notifications/service.py` |
| Lock-screen approval | Passed on a real iPhone on cellular on 2026-09-14: approval notification at 76 s, approved from the lock screen at 87 s, "Done" at 92 s. The slow start was the model waking up, not the notification path. | Repo: `docs/phase2-results.md` |
| Web push | None. "No push through the web app itself (ntfy covers notifications)" is listed as a known gap, a scope choice and not a platform finding. | Repo: `docs/phase2-results.md` |
| Auth | Tailscale identity for the owner and members on the same origin; origin-bound single-use `hp-` pairing codes (10-minute TTL) for a separately hosted app; App, device and owner tokens. | Repo: `docs/app-api.md`, `docs/orca-study.md` |
| Service worker | Registers only over HTTPS, so the loopback test instance has no service worker and CI cannot see offline behaviour. | Repo: `docs/ui-testing.md` |

## 1. What the PWA cannot do that a native client must

The question is not "what would be nice natively" but "what fails or is missing in the PWA that a native client would
fix". Checked one by one, only some items are real platform limits. The table gives the verdict first.

| Capability | PWA verdict | Evidence |
| --- | --- | --- |
| Push without ntfy | **Not a hard limit.** Home Screen web apps can receive Web Push on iOS 16.4 and later. Stables does not implement it. The open question is action buttons, which would be needed for lock-screen approvals. | Web: [WebKit, Web Push for Web Apps on iOS and iPadOS](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/). Unverified: reports on the [Apple developer forum](https://developer.apple.com/forums/thread/726793) say custom notification actions were ignored on iOS 16.4; nothing found says whether current iOS honours them. |
| Background refresh | **Real limit.** iOS Safari does not support Background Sync or Periodic Background Sync, so a PWA only updates while it is open or when a push wakes its service worker. | Web: [caniuse, Background Sync API](https://caniuse.com/background-sync) lists iOS Safari as unsupported through 26.5 (an unofficial table). |
| Approvals from the lock screen | **Works today through ntfy, not through the PWA.** The PWA's own notifications would depend on the action-button question above. | Repo: `docs/phase2-results.md` (87 s, passed); `harness_modules/notifications/service.py` (ntfy iOS does not dismiss a notification after an action button, ntfy issue #1728, so the daemon replaces it). |
| Biometric unlock | **Possible in principle, not built.** Stables has no app-level lock; access is the tailnet identity or a pairing token. WebAuthn/passkeys may work in a Home Screen web app, but this was not checked. | Repo: `docs/app-api.md` (no lock). Unverified: WebAuthn behaviour in an installed iOS web app. |
| Offline shell | **Not a platform limit.** #368 (white screen with Airplane Mode on, found on a real iPhone) was a boot-logic bug: the service worker cache was complete, and `fetchMe()` turned a network error into a guest identity. PR #369 fixed it (2026-10-04). The owner's iOS re-check is still open. | Repo: #368 and its comments, `tests/web_boot_requests.mjs` scenarios named there, `docs/ui-testing.md` (no service worker on loopback HTTP). |

What this means:

1. **Only background refresh is a clean "PWA cannot".** Even natively it is weak: the system decides when a refresh
   runs, the budget is seconds, and nothing runs after the user force-quits the app (Web: Flutter Workmanager's
   troubleshooting notes, [docs.page](https://docs.page/fluttercommunity/flutter_workmanager/troubleshooting), which
   describe the same iOS `BGTaskScheduler` limits for any native app). Push is the reliable wake-up either way.
2. **Push and lock-screen approval are already met by ntfy.** A native client changes how the push is sent, not
   whether it works (section 2).
3. **Offline and biometric are product gaps, not limits.** The offline gap is fixed pending the owner's check. The
   biometric gap has no stated requirement in the repo.
4. **The strongest remaining argument for native is the unverified one**: that the PWA can never present Approve/Deny
   on the lock screen without ntfy. One afternoon on the iPhone with a throwaway Web Push test would settle it. That is
   a check, not code in this repo, and it is the cheapest thing that could change the recommendation.

## 2. Swift support and Flutter support, per capability

Both columns assume an iOS-only app that talks to `/api/v1` with the pairing flow in `docs/app-api.md`.

| Capability | Native Swift | Flutter | Native code Flutter still needs |
| --- | --- | --- | --- |
| Push without ntfy | APNs directly. The daemon holds an APNs auth key and signs requests to Apple. Needs a paid Apple Developer Program membership ($99 per year, Web: [Apple membership details](https://developer.apple.com/programs/whats-included/)) and a bundle identifier. | Same APNs path. A Flutter app registers for remote notifications through a plugin (commonly Firebase Messaging, which adds Google's FCM service between the daemon and Apple) or through its own platform channel. | Registration and token delivery are platform code in `AppDelegate`; with a plugin they are hidden but still run in Swift. Unverified in this study: exact plugin behaviour. |
| Background refresh | `BGAppRefreshTask` and silent push (`content-available`). Both are best-effort and throttled. | Plugin wrapper around `BGTaskScheduler` (Workmanager). The Dart isolate starts only when iOS relaunches the app. | A permitted-identifier entry in `Info.plist` and a handler registered before launch finishes: native configuration. Web: [Flutter Workmanager troubleshooting](https://docs.page/fluttercommunity/flutter_workmanager/troubleshooting). |
| Lock-screen approvals | `UNNotificationCategory` and `UNNotificationAction`, handled in `UNUserNotificationCenterDelegate`. The `authenticationRequired` option makes iOS ask for an unlock first (Web: [Apple docs](https://developer.apple.com/documentation/usernotifications/unnotificationaction)). The handler can call the daemon with a token read from the Keychain, instead of the per-approval secret ntfy uses. | A notification plugin can register categories and report action taps to Dart. | Handling an action while the app is not running is the weak point. Whether the plugin's Dart callback runs reliably with the app terminated, and for how long, was not established (searches returned nothing). Plan on a Swift delegate. Unverified. |
| Biometric unlock | `LocalAuthentication` with a Keychain item whose access control requires biometry; Face ID needs `NSFaceIDUsageDescription`. | `local_auth` for the prompt and `flutter_secure_storage` for the Keychain. `local_auth` only reports pass or fail and stores nothing; gating the stored secret behind the prompt is a separate step. | `NSFaceIDUsageDescription` in `Info.plist` (Web: [`local_auth` source](https://flutter.googlesource.com/mirrors/packages/+/refs/tags/url_launcher_macos-v3.0.7/packages/local_auth/local_auth)). Binding a secret to biometry needs a native Keychain access-control flag, or a young third-party package (the search found `biometric_security`, pre-1.0). |
| Offline shell | The UI is in the app bundle, so it opens offline by construction. Data views need a local cache: SwiftData or a plain file store, written by hand. | Same: UI in the bundle, cache by hand (Dart packages). | None. |

**Consequence for the shared constraint in `docs/orca-study.md`**: the native client must never carry a pairing code or
token in a custom URL scheme (another app can register the same scheme). Use a typed or QR code redeemed in the app,
or verified associated-domain links. This applies to Swift and Flutter alike.

**Where Flutter would still need Swift.** Three of the five capabilities (remote push registration, notification
actions while terminated, Keychain access control) land in `AppDelegate` or a notification delegate whichever
framework is chosen. In an app whose reasons to exist are those capabilities, Flutter would carry its own runtime and a
second language (Dart) on top of the Swift it cannot avoid. Flutter would earn its place on an app whose value is a
large screen UI it can share across two OSes, and the Android half is out of scope here.

## 3. Build setup and cost

| Item | Native Swift | Flutter | Source |
| --- | --- | --- | --- |
| Toolchain | Xcode, from the App Store, with the command-line tools selected and the licence accepted. This is what #31 already verifies. | Everything Swift needs, plus the Flutter SDK with its bundled Dart SDK, CocoaPods and `flutter doctor`. iOS builds still run through Xcode. | Web: [Flutter, iOS setup on macOS](https://docs.flutter.dev/platform-integration/ios/install-ios/install-ios-from-macos) |
| Licence | Xcode and the Apple SDKs are under Apple's licence. The developer programme costs $99 per year for TestFlight, push and distribution. | The same Apple costs. Flutter is BSD-3-Clause (stated on #672; the `LICENSE` file was not opened here). The Dart SDK is downloaded from Google; third-party registries list it as BSD-3-Clause, and the download terms were not read. | Web: [Apple membership details](https://developer.apple.com/programs/whats-included/). Unverified: Dart download terms. |
| MacBook (16 GB, limited) | Xcode and the simulator. | Xcode, the simulator and the Dart toolchain, with a hot-reload session on top of them. Not measured. | Unverified |
| Second language in the repo | Swift. | Dart, and Swift for the platform code above. | Repo: Python, JavaScript and PowerShell today. |
| CI | A macOS runner for `xcodebuild`. The Server's CI is Ubuntu, so this is a new runner class. | A macOS runner for `flutter build ios`, plus the Flutter SDK install. | Repo: `.github/workflows/ci.yml` runs on the jobs listed in `AGENTS.md`; none builds iOS. Web: Apple and Flutter docs above. |
| End-to-end tests | XCUITest. | Flutter's `integration_test`, or Patrol, which adds native automation for permission dialogs and notifications on top of XCTest (Web: [Patrol FAQ](https://www.mintlify.com/leancodepl/patrol/resources/faq)). The issue says the e2e framework piloted as tester-army/e2e supports Flutter; that repository is not in this workspace and the claim is **unverified** here. | Repo: today's journeys are Playwright on iPhone SE (WebKit) and Pixel 7 (Chromium) emulation, `tools/ui-tests/playwright.config.ts`. |
| Maintenance | Apple's yearly SDK and deployment-target changes, one language. | The same changes plus Flutter's own release cadence and every plugin's. Each plugin above is a place a dependency can lag behind iOS. | Unverified |
| API contract | Generate or hand-write a client from `tools/openapi/openapi.json`. | The same, in Dart. | Repo: `docs/web.md` (Stables is typed from that document). |

Cost that is the same for both: the Apple membership, a macOS runner, a native client of the app API, and the
tailnet requirement (the phone must run Tailscale, as it does for the PWA and the ntfy action buttons).

## 4. Recommendation

**Stay PWA-only for now. If a native iOS client is ever built, build it in Swift, not Flutter.**

Reasons:

1. **The audited gaps are mostly not platform limits.** Offline was a boot bug, already fixed, with the owner's iOS
   re-check still open. Lock-screen approvals already pass through ntfy. Only background refresh is a PWA limit, and it
   is weak on native too.
2. **The native case turns on one unverified question**: whether an installed iOS web app can show Approve/Deny on the
   lock screen. Until that is answered on a device, building a second client is paying to solve a problem that may not
   exist.
3. **A native client does not remove the third parties; it moves them.** ntfy's iOS delivery depends on a poll request
   to ntfy.sh when self-hosted (Web: [ntfy known issues](https://docs.ntfy.sh/known-issues/)). Direct APNs depends on
   Apple and needs an APNs key held by the daemon, which is a new secret in the Server's trust boundary.
4. **If native, Swift.** The features that justify a native app are the ones Flutter hands back to Swift. The app is
   iOS-only with Android out of scope, so Flutter's main payoff (one UI for two platforms) is not in play. Swift adds one
   toolchain, Xcode, which #31 already covers. Flutter adds the Dart SDK, CocoaPods and plugin upkeep on a limited 16 GB
   machine.

**What would change this:**

| Trigger | Moves the recommendation to |
| --- | --- |
| A real-iPhone test shows installed-PWA Web Push cannot show Approve/Deny on the lock screen, or delivers unreliably, and the owner wants to drop ntfy. | Native Swift. |
| The owner's iOS re-check of #368 fails again, or a second offline or eviction failure appears on a real device that a boot fix cannot reach. | Native Swift, or a fix in Stables first. |
| The owner wants a biometric gate on the app and a passkey test in the installed PWA fails. | Native Swift. |
| Android becomes in scope with the same feature set. | Re-run this study; Flutter's shared UI starts to count against the Swift-plus-Kotlin pair. |
| Apple's yearly fee, a macOS runner or the APNs key in the daemon is unacceptable. | Stay PWA-only. |

## What #31 should become

Not edited by this PR. If the recommendation is accepted, #31 should keep its scope (a user-machine check of Xcode
and the command-line tools, the licence, the active developer directory and a minimal Swift build, recording the
supported macOS and Xcode versions) but say that it waits on the triggers above, and that the assumed direction is
Swift, with no Flutter or Dart install.

## Not verified

- Nothing was tested on a device, the MacBook, Xcode or a macOS runner.
- Whether current iOS shows Web Push action buttons, and WebAuthn behaviour in an installed iOS web app.
- Whether Flutter's notification plugins run Dart on an action tap with the app terminated.
- The Dart SDK download terms, Flutter's `LICENSE` file, and plugin licences.
- Memory and disk cost of Flutter next to Xcode on the 16 GB MacBook.
- The tester-army/e2e Flutter support claim (the repository is not here).
- Web search results were read as summaries; pages are linked so the owner can check the wording.
