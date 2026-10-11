// Agent Harness web app: plain ES module, no build step. Hash routes:
//   #/                       redirects to #/chat (owner) or #/agents
//   #/chat[/<id>]            Chat home: welcome state, or a durable non-agent conversation
//   #/agents                 agent session list
//   #/new                    new task (templates)
//   #/s/<id>                 session transcript (live)
//   #/s/<id>/approval/<aid>  same, focused on one approval (notification deep link)
//   #/s/<id>/changes         diff viewer
//   #/s/<id>/info            session details
//   #/actions[/<tab>]        owner actions: resources, accounts, remote-control, disk (from Settings → Server)
//   #/profile                Profile tab: identity plus Settings menu
//   #/settings               the same menu under the header's Settings gear
//   #/profile/account        icon picker, account info, connection details
//   #/profile/<section>      a Settings page (appearance, notifications, backends, …)
//   #/profile/{accounts,disk,remote-control} redirect to #/actions/<tab>
//   #/images                 image generation and gallery
//   #/images/<id>            one result (prompt, metadata, Another one)
//   #/images/<id>/edit       masked inpainting / photo edit
//   #/images/<id>/full       in-app fullscreen viewer
//   #/jobs[/new|/<id>]       scheduled jobs
//   #/tasks[/…]              redirects to #/jobs[/…] (the old label for scheduled work)
//   #/signin[/failed]        Google sign-in for a household member on a Tailscale-admitted device (issue #64)
// At 1280 px+ #/agents and #/s/<id>[/…] share one split view: the list beside the open session (lib/layout.mjs, #563).

import { agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL } from "./client.mjs";
import { h, fill, append } from "./lib/dom.mjs";
import { createSession } from "./lib/session.mjs";
import { mountChrome } from "./lib/chrome.mjs";
import { mountStream, validId } from "./lib/stream.mjs";
import { mountDaemonFiles } from "./lib/files.mjs";
import { mountSignIn } from "./lib/signin.mjs";
import { mountRouter } from "./lib/router.mjs";
import { mountTabs } from "./lib/tabs.mjs";
import { mountUpdate } from "./lib/update.mjs";
import { mountKeys } from "./lib/keys.mjs";
import { startBoot } from "./lib/boot.mjs";
import { createWarmModel } from "./lib/warm-model.mjs";
import { TERMINAL, REVIEW_LABEL, progressBar, reviewBadge, badge, jobStatusBadge } from "./lib/widgets.mjs";
import { mountImages } from "./pages/images.mjs";
import { mountJobs } from "./pages/jobs.mjs";
import { mountActions } from "./pages/actions.mjs";
import { mountSessionInfo } from "./pages/session-info.mjs";
import { mountDaemonSettings } from "./pages/daemon-settings.mjs";
import { mountProfile } from "./pages/profile.mjs";
import { mountNewTask } from "./pages/new-task.mjs";
import { mountSessions } from "./pages/sessions.mjs";
import { mountChat } from "./pages/chat.mjs";
import { mountSession } from "./pages/session.mjs";

const byId = (id) => document.getElementById(id);
const els = {
  $app: byId("app"), $title: byId("title"), $back: byId("back"), $conn: byId("conn"),
  $profileIcon: byId("profile-icon"), $fabHost: byId("fab-host"), $fab: byId("fab"),
  $tabBar: byId("tab-bar"), $settings: byId("settings-btn"),
};
const { $app, $conn, $profileIcon } = els;
const browser = globalThis;

// ---------- shell: identity, chrome, tab bar, streams, sign-in, router ----------
let storage = null;
try { storage = browser.localStorage; } catch (_) { /* storage blocked */ }
const session = createSession({ agentHarnessWeb, storage });
const { api, fetchMe, ownerSurface, isGuest, isMember, isOwner, canChat } = session;
const chrome = mountChrome({ els, browser, session });
const { layoutBar, setHeader, showListAction, toast, setConnState, onConnState } = chrome;
const stream = mountStream({ agentHarnessWeb, isBlocked: session.isBlocked, setConnState, ownerSurface, isGuest, browser });
const tabs = mountTabs({ els, session, browser, chrome, stream });
const { openStream } = stream;
const { daemonImage, downloadDaemonFile } = mountDaemonFiles({ agentHarnessWeb, isBlocked: session.isBlocked, ownerSurface, toast, browser });
const signin = mountSignIn({ els, api, getWebAuth: session.getWebAuth, toast, browser });
const { startGoogle } = signin;

// Route registration: the router reads the page views lazily, because the pages below are mounted after it.
const { go, route, onLeave, closeSplit } = mountRouter({ els, session, chrome, tabs, signin, stream, toast, browser,
  views: () => ({ viewChat, viewList, viewNew, viewActions, viewProfile, viewImages, viewImage, viewImageEdit, viewImageFull,
    viewJobs, viewJob, viewSession }) });
const warmModel = createWarmModel({ api, session });

// Mounted before the pages so Settings' version row can reload into a newer bundle (#512).
const build = { WEB_BUILD_ID, WEB_PROTOCOL };
const { checkCompatibility, reloadAndUpdate } = mountUpdate({ els, agentHarnessWeb, session, chrome, tabs, route, closeSplit, build, browser });

// ---------- pages ----------
const { daemonSettingsCard } = mountDaemonSettings({ h, fill, append, api, toast, isGuest, location });
const { viewProfile, copyBox, githubConnectionCard, readAppIcon, applyAppIcon, applyTheme, applyTextSize } = mountProfile({ $app, $conn, $profileIcon,
  layoutBar, setHeader, h, fill, append, api, getWebAuth: session.getWebAuth, startGoogle, agentHarnessWeb, isGuest, isMember, isOwner, toast, go, route, daemonSettingsCard,
  build, reloadAndUpdate, onConnState, browser });
applyTheme();
applyTextSize();

const { viewInfo } = mountSessionInfo({ $app, h, append, copyBox, downloadDaemonFile });
const { viewChat } = mountChat({ $app, h, fill, append, api, setHeader, toast, go, validId, canChat, onLeave, openStream, ownerSurface, badge,
  TERMINAL, agentHarnessWeb, browser });
const { viewSession } = mountSession({ $app, h, fill, append, api, setHeader, toast, go, route, validId, isGuest, isMember, isOwner, onLeave, badge, reviewBadge,
  progressBar, openStream, layoutBar, viewInfo, downloadDaemonFile, TERMINAL, agentHarnessWeb, browser, announceChange: stream.announceChange,
  onDaemonChange: stream.onDaemonChange });
const { viewNew, confirmGpuQueue } = mountNewTask({ $app, h, fill, append, api, setHeader, toast, route, isMember, isOwner, onLeave,
  githubConnectionCard, warmModel, browser });
const { viewImages, viewImage, viewImageEdit, viewImageFull } = mountImages({ $app, h, fill, append, api, setHeader, toast, go, route, isGuest, isMember, onLeave,
  progressBar, confirmGpuQueue, daemonImage, downloadDaemonFile, location });
const { viewJobs, viewJob } = mountJobs({ $app, h, fill, append, api, setHeader, showListAction, toast, go, route, isGuest,
  confirmGpuQueue, badge, jobStatusBadge, location, onLeave, onDaemonChange: stream.onDaemonChange, browser });
const { viewList } = mountSessions({ $app, h, fill, append, api, setHeader, showListAction, onLeave, isMember, isGuest, badge, reviewBadge, REVIEW_LABEL,
  jobStatusBadge, onDaemonChange: stream.onDaemonChange, onDaemonState: stream.onDaemonState, browser });
const { viewActions } = mountActions({ $app, h, fill, append, api, setHeader, toast, go, isGuest, isMember, onLeave, copyBox, progressBar });

// Keyboard shortcuts (#571): `?` lists them; none fires while focus is in a field.
mountKeys({ browser, go, role: () => session.getMe()?.role });

// ---------- boot ----------
async function loadProfileIcon() {
  if (session.isBlocked()) return;
  try { $profileIcon.textContent = (await api("/profile")).emoji; } catch (_) { /* offline */ }
}

if ("serviceWorker" in navigator && location.protocol === "https:") {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") void checkCompatibility({ foreground: true });
});

startBoot({ window, checkCompatibility, fetchMe, adoptMe: (me) => { session.setMe(me); session.setBootMe(me); },
  paintGuestChrome: chrome.paintGuestChrome, loadProfileIcon, readAppIcon, applyAppIcon, route });
