// Agent Harness web app: plain ES module, no build step. Hash routes:
//   #/                       redirects to #/chat (owner) or #/agents
//   #/chat[/<id>]            Chat home: welcome state, or a durable non-agent conversation
//   #/agents                 agent session list
//   #/new                    new task (templates)
//   #/s/<id>                 session transcript (live)
//   #/s/<id>/approval/<aid>  same, focused on one approval (notification deep link)
//   #/s/<id>/changes         diff viewer
//   #/s/<id>/info            session details
//   #/actions[/<tab>]        owner actions: resources, accounts, remote-control, disk
//   #/profile                identity plus Settings menu
//   #/profile/account        icon picker, account info, connection details
//   #/profile/<section>      a Settings page (appearance, notifications, backends, …)
//   #/profile/{accounts,disk,remote-control} redirect to #/actions/<tab>
//   #/images                 image generation and gallery
//   #/images/<id>            one result (prompt, metadata, Another one)
//   #/images/<id>/edit       masked inpainting / photo edit
//   #/images/<id>/full       in-app fullscreen viewer
//   #/jobs[/new|/<id>]       scheduled jobs
//   #/signin[/failed]        Google sign-in for a household member on a Tailscale-admitted device (issue #64)

import { agentHarnessWeb, WEB_BUILD_ID, WEB_PROTOCOL } from "./client.mjs";
import { h, fill, append } from "./lib/dom.mjs";
import { createSession } from "./lib/session.mjs";
import { mountChrome } from "./lib/chrome.mjs";
import { mountStream, validId } from "./lib/stream.mjs";
import { mountDaemonFiles } from "./lib/files.mjs";
import { mountSignIn } from "./lib/signin.mjs";
import { mountRouter } from "./lib/router.mjs";
import { mountDrawer } from "./lib/drawer.mjs";
import { mountUpdate } from "./lib/update.mjs";
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
  $app: byId("app"), $title: byId("title"), $back: byId("back"), $conn: byId("conn"), $feature: byId("feature-nav"),
  $profileIcon: byId("profile-icon"), $fabHost: byId("fab-host"), $fab: byId("fab"),
  $menu: byId("menu-btn"), $drawer: byId("nav-drawer"), $scrim: byId("drawer-scrim"), $drawerChats: byId("drawer-chats"),
};
const { $app, $conn, $profileIcon } = els;
const browser = globalThis;

// ---------- shell: identity, chrome, streams, sign-in, router, drawer ----------
let storage = null;
try { storage = browser.localStorage; } catch (_) { /* storage blocked */ }
const session = createSession({ agentHarnessWeb, storage });
const { api, fetchMe, ownerSurface, isGuest, isMember, isOwner, canChat } = session;
const chrome = mountChrome({ els, browser, session });
const { layoutBar, setHeader, showFab, toast, setConnLive } = chrome;
const stream = mountStream({ agentHarnessWeb, isBlocked: session.isBlocked, setConnLive, ownerSurface, isGuest, browser });
const { openStream } = stream;
const { daemonImage, downloadDaemonFile } = mountDaemonFiles({ agentHarnessWeb, isBlocked: session.isBlocked, ownerSurface, toast, browser });
const signin = mountSignIn({ els, api, getWebAuth: session.getWebAuth, toast, browser });
const { startGoogle } = signin;

// Route registration: the router reads the page views lazily, because the pages below are mounted after it.
const { go, route, onLeave } = mountRouter({ els, session, chrome, signin, stream, toast, browser,
  views: () => ({ viewChat, viewList, viewNew, viewActions, viewProfile, viewImages, viewImage, viewImageEdit, viewImageFull,
    viewJobs, viewJob, viewSession }) });
mountDrawer({ els, session, browser });
const warmModel = createWarmModel({ api, session });
const confirmText = (m) => confirm(m);

// ---------- pages ----------
const { daemonSettingsCard } = mountDaemonSettings({ h, fill, append, api, toast, isGuest, location, confirm: confirmText });
const { viewProfile, copyBox, githubConnectionCard, readAppIcon, applyAppIcon, applyTheme, applyTextSize } = mountProfile({ $app, $conn, $profileIcon,
  layoutBar, setHeader, h, fill, append, api, getWebAuth: session.getWebAuth, startGoogle, agentHarnessWeb, isGuest, isMember, toast, go, route, daemonSettingsCard, browser });
applyTheme();
applyTextSize();

const { viewInfo } = mountSessionInfo({ $app, h, append, copyBox, downloadDaemonFile });
const { viewChat } = mountChat({ $app, h, fill, append, api, setHeader, toast, go, validId, canChat, onLeave, openStream, ownerSurface, badge,
  TERMINAL, agentHarnessWeb, browser });
const { viewSession } = mountSession({ $app, h, fill, append, api, setHeader, toast, go, route, validId, isGuest, isMember, isOwner, onLeave, badge, reviewBadge,
  progressBar, openStream, layoutBar, viewInfo, TERMINAL, agentHarnessWeb, browser });
const { viewNew, confirmGpuQueue } = mountNewTask({ $app, h, fill, append, api, setHeader, toast, route, isMember, isOwner, onLeave,
  githubConnectionCard, warmModel, browser });
const { viewImages, viewImage, viewImageEdit, viewImageFull } = mountImages({ $app, h, fill, append, api, setHeader, toast, go, route, isGuest, isMember, onLeave,
  progressBar, confirmGpuQueue, daemonImage, downloadDaemonFile, location, confirm: confirmText });
const { viewJobs, viewJob } = mountJobs({ $app, h, fill, append, api, setHeader, showFab, toast, go, route, isGuest,
  confirmGpuQueue, badge, jobStatusBadge, location, confirm: confirmText });
const { viewList } = mountSessions({ $app, h, fill, append, api, setHeader, showFab, onLeave, isMember, isGuest, badge, reviewBadge, REVIEW_LABEL,
  jobStatusBadge, openStream, ownerSurface, agentHarnessWeb, browser });
const { viewActions } = mountActions({ $app, h, fill, append, api, setHeader, toast, go, isGuest, isMember, onLeave, copyBox, progressBar });

// ---------- boot ----------
const { checkCompatibility } = mountUpdate({ els, agentHarnessWeb, session, chrome, route, build: { WEB_BUILD_ID, WEB_PROTOCOL }, browser });

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
