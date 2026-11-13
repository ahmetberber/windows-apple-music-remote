#!/usr/bin/env python3
"""Apple Music Remote for Windows.

A tiny self-hosted web remote for the Apple Music app on Windows. Serves a
mobile-friendly web UI on your local network with now-playing info, transport
controls, volume, in-track seeking, shuffle/repeat, a playlist browser,
catalog search and the Up Next queue.

The Apple Music app for Windows does not support Apple's iTunes Remote app
(it exposes no DACP service), so this project bridges the gap with three
Windows-native mechanisms:

- SMTC (System Media Transport Controls, via ``winrt``) for playback state,
  track metadata, artwork, and play/pause/next/previous.
- Core Audio (via ``pycaw``) for reading and setting the system volume.
- UI Automation (via ``uiautomation``) for everything Apple Music's SMTC
  integration ignores: seeking, shuffle, repeat, and playlist browsing.

Architecture: each mechanism lives on its own long-lived worker thread that
keeps its COM/WinRT objects alive, and publishes into one shared state
snapshot (``Hub``). Phones receive that snapshot over Server-Sent Events, so
nothing is polled per request and the UI updates the moment Windows reports
a change.

Usage (must run on Windows with the Apple Music app installed)::

    pip install -r requirements.txt
    python apple-music-remote.py [--port 8765]

Then open the printed URL in your phone's browser (same Wi-Fi network).
See README.md for firewall setup and limitations.
"""

import argparse
import asyncio
import ctypes
import hashlib
import json
import queue
import re
import socket
import threading
import time
from concurrent.futures import Future
from ctypes import wintypes
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import NamedTuple, Optional
from urllib.parse import parse_qs, urlparse

import uiautomation
from comtypes import CoInitialize
from pycaw.pycaw import AudioUtilities
from uiautomation import uiautomation as _uia
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winrt.windows.storage.streams import Buffer, InputStreamOptions

DEFAULT_PORT = 8765
APPLE_MUSIC_WINDOW = "Apple Music"
APPLE_MUSIC_WINDOW_CLASS = "WinUIDesktopWin32WindowClass"
SCRUBBER_AUTOMATION_ID = "LCDScrubber"
QUEUE_LIST_AUTOMATION_ID = "PlayQueueListView"
TRACK_DURATION_RE = re.compile(r"^\d+:\d{2}(:\d{2})?$")

# Repeat state is read from the transport-bar button's accessible name.
# These names come from the English UI; other locales fall back to "none"
# (the cycle button itself still works regardless of locale).
REPEAT_STATES = {"Do Not Repeat": "none", "Repeat All": "all", "Repeat One": "one"}

PAGE = r"""<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover,user-scalable=no">
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-status-bar-style content=black-translucent>
<meta name=apple-mobile-web-app-title content="Music Remote">
<meta name=theme-color content="#000">
<title>Apple Music Remote</title>
<style>
:root{--acc:#ff4e6b;--dim:rgba(255,255,255,.58);--faint:rgba(255,255,255,.12);
  --st:env(safe-area-inset-top,0px);--sb:env(safe-area-inset-bottom,0px);--tab:54px;--mini:62px}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
[hidden]{display:none!important}
html,body{height:100%;overflow:hidden;overscroll-behavior:none}
body{font:16px/1.25 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:#fff;background:#000;
  -webkit-user-select:none;user-select:none;touch-action:manipulation;-webkit-font-smoothing:antialiased}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer;touch-action:manipulation}
svg{display:block;fill:currentColor}

/* Backdrop: a small element blurred cheaply, then scaled up by the compositor. */
.bg{position:fixed;left:50%;top:50%;width:60vmax;height:60vmax;margin:-30vmax 0 0 -30vmax;
  background:#111 center/cover no-repeat;filter:blur(22px) saturate(1.7);transform:scale(2.6);
  opacity:0;transition:opacity .7s;will-change:opacity}
.bg.on{opacity:1}
#shade{position:fixed;inset:0;background:linear-gradient(rgba(0,0,0,.38),rgba(0,0,0,.72))}
body.sub #shade{background:rgba(8,8,10,.86)}

.view{position:fixed;left:0;right:0;top:0;bottom:calc(var(--tab) + var(--sb));display:none;flex-direction:column}
.view.on{display:flex;animation:in .2s ease-out}
@keyframes in{from{opacity:0;transform:translateY(6px)}}

/* ---- Now Playing ---- */
#v-now{max-width:460px;margin:0 auto;padding:calc(var(--st) + 14px) 26px 8px;gap:clamp(8px,2.1dvh,20px)}
.artwrap{flex:1;min-height:0;container-type:size;display:flex;align-items:center;justify-content:center}
#artbox{width:min(100cqw,100cqh);height:min(100cqw,100cqh);border-radius:14px;overflow:hidden;
  background:rgba(255,255,255,.08);box-shadow:0 18px 50px rgba(0,0,0,.55);position:relative;
  transition:transform .45s cubic-bezier(.3,1.35,.5,1)}
body.paused #artbox{transform:scale(.84)}
#art{width:100%;height:100%;object-fit:cover;display:block}
#artbox svg{position:absolute;inset:30%;width:40%;height:40%;opacity:.3;display:none}
body.noart #art{display:none}
body.noart #artbox svg{display:block}
#t{font-size:1.32rem;font-weight:700;letter-spacing:-.01em}
#a{font-size:1.05rem;color:var(--dim);margin-top:2px}
#t,#a,.rt b,.rt span,.bt{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

.sl{height:30px;display:flex;align-items:center;touch-action:none;cursor:pointer;flex:1}
.tr{flex:1;height:7px;border-radius:4px;background:rgba(255,255,255,.22);overflow:hidden;transition:height .16s,border-radius .16s}
.sl.drag .tr{height:13px;border-radius:7px}
.fl{height:100%;background:rgba(255,255,255,.92);transform-origin:0 50%;transform:scaleX(var(--v,0));will-change:transform}
.sl.drag .fl{background:#fff}
#times{display:flex;justify-content:space-between;font-size:.74rem;color:var(--dim);
  font-variant-numeric:tabular-nums;margin-top:-3px}
body.nodur #prog{visibility:hidden}

#ctrl{display:flex;align-items:center;justify-content:space-around;padding:0 6px}
#ctrl button{width:72px;height:72px;border-radius:50%;display:grid;place-items:center;transition:transform .12s,background .2s}
#ctrl button:active{transform:scale(.86);background:var(--faint)}
#ctrl svg{width:38px;height:38px}
#pp svg{width:52px;height:52px}
#vol{display:flex;align-items:center;gap:10px}
#vol button{width:36px;height:36px;display:grid;place-items:center;color:var(--dim)}
#vol svg{width:20px;height:20px}
#vol>svg{color:var(--dim);flex:none}
body.muted #vol .sl{opacity:.35}
body.muted #mute{color:var(--acc)}
#acts{display:flex;justify-content:space-around;padding-bottom:2px}
#acts button{width:64px;height:40px;border-radius:12px;display:grid;place-items:center;color:var(--dim);transition:background .2s,color .2s,transform .12s}
#acts button:active{transform:scale(.9)}
#acts svg{width:22px;height:22px}
#acts button.on{color:var(--acc);background:rgba(255,78,107,.16)}

/* ---- Lists ---- */
.scroll{overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain}
#v-lib,#v-src{max-width:620px;margin:0 auto}
.page{flex:1;min-height:0;display:flex;flex-direction:column}
.head{padding:calc(var(--st) + 10px) 18px 8px;flex:none}
h1{font-size:1.9rem;font-weight:800;letter-spacing:-.02em}
.bar{display:flex;align-items:center;gap:4px;margin:0 -10px 4px}
.bar button{width:44px;height:44px;display:grid;place-items:center;flex:none;border-radius:50%}
.bar button:active{background:var(--faint)}
.bar svg{width:24px;height:24px}
.bt{flex:1;font-weight:700;font-size:1.08rem;text-align:center}
.spinning svg{animation:r .8s linear infinite}
.field{width:100%;height:40px;padding:0 14px;border-radius:11px;border:0;outline:0;font:inherit;color:#fff;
  background:rgba(255,255,255,.13);-webkit-appearance:none;appearance:none;-webkit-user-select:text;user-select:text;margin-top:8px}
.field::placeholder{color:rgba(255,255,255,.45)}
.field::-webkit-search-cancel-button{-webkit-appearance:none}
.list{flex:1;min-height:0;padding:0 12px calc(var(--mini) + 18px)}
.row{display:flex;align-items:center;gap:12px;min-height:56px;padding:8px;border-radius:10px;
  cursor:pointer;content-visibility:auto;contain-intrinsic-size:auto 56px;position:relative}
.row+.row::before{content:"";position:absolute;left:8px;right:8px;top:0;border-top:.5px solid rgba(255,255,255,.1)}
.row:active{background:rgba(255,255,255,.1)}
.row.busy{opacity:.5;pointer-events:none}
.row.static{cursor:default}
.row.static:active{background:none}
.badge{width:44px;height:44px;border-radius:9px;display:grid;place-items:center;flex:none;
  font-weight:700;font-size:1.1rem;background:linear-gradient(135deg,#fb5c74,#7a5cfa)}
.rt{flex:1;min-width:0}
.rt b{display:block;font-size:.98rem;font-weight:500}
.rt span{display:block;font-size:.82rem;color:var(--dim);margin-top:2px}
.row.playing b{color:var(--acc);font-weight:600}
.dur{font-size:.8rem;color:var(--dim);font-variant-numeric:tabular-nums;flex:none}
.chev{width:16px;height:16px;color:rgba(255,255,255,.35);flex:none}
.spin{width:26px;height:26px;border:3px solid rgba(255,255,255,.18);border-top-color:#fff;border-radius:50%;
  animation:r .8s linear infinite;margin:48px auto 14px}
@keyframes r{to{transform:rotate(360deg)}}
.hint{text-align:center;color:var(--dim);font-size:.9rem;padding:18px 26px;line-height:1.4}
.count{color:var(--dim);font-size:.82rem;padding:2px 8px 8px}

/* ---- Mini player + tab bar ---- */
#mini{position:fixed;left:8px;right:8px;bottom:calc(var(--tab) + var(--sb) + 8px);height:var(--mini);max-width:604px;margin:0 auto;
  display:none;align-items:center;gap:10px;padding:0 6px 0 9px;border-radius:14px;overflow:hidden;cursor:pointer;
  background:rgba(44,44,48,.82);-webkit-backdrop-filter:blur(24px) saturate(1.6);backdrop-filter:blur(24px) saturate(1.6);
  box-shadow:0 8px 26px rgba(0,0,0,.45)}
body.sub #mini{display:flex}
#mart{width:44px;height:44px;border-radius:8px;object-fit:cover;background:rgba(255,255,255,.1);flex:none}
body.noart #mart{visibility:hidden}
#mini button{width:44px;height:44px;display:grid;place-items:center;flex:none;border-radius:50%}
#mini button:active{background:var(--faint)}
#mini svg{width:26px;height:26px}
#mprog{position:absolute;left:0;right:0;bottom:0;height:2.5px;background:rgba(255,255,255,.14)}
#mprog i{display:block;height:100%;background:var(--acc);transform-origin:0 50%;transform:scaleX(var(--v,0))}
#tabs{position:fixed;left:0;right:0;bottom:0;height:calc(var(--tab) + var(--sb));padding-bottom:var(--sb);display:flex;
  background:rgba(18,18,20,.8);-webkit-backdrop-filter:blur(24px) saturate(1.6);backdrop-filter:blur(24px) saturate(1.6);
  border-top:.5px solid rgba(255,255,255,.12)}
#tabs button{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
  font-size:.66rem;font-weight:600;color:rgba(255,255,255,.5);transition:color .15s}
#tabs svg{width:25px;height:25px}
#tabs button.on{color:var(--acc)}

/* ---- Queue sheet ---- */
#dim{position:fixed;inset:0;background:rgba(0,0,0,.5);opacity:0;pointer-events:none;transition:opacity .25s}
#sheet{position:fixed;left:0;right:0;bottom:0;top:calc(var(--st) + 56px);max-width:620px;margin:0 auto;display:flex;flex-direction:column;
  background:rgba(30,30,33,.94);-webkit-backdrop-filter:blur(30px);backdrop-filter:blur(30px);border-radius:18px 18px 0 0;
  transform:translateY(105%);transition:transform .32s cubic-bezier(.3,.9,.3,1);visibility:hidden}
body.sheet #dim{opacity:1;pointer-events:auto}
body.sheet #sheet{transform:none;visibility:visible}
.grab{width:38px;height:5px;border-radius:3px;background:rgba(255,255,255,.3);margin:8px auto 2px;flex:none}
#sheet .bar{margin:0 6px 2px 18px}
#sheet .bt{text-align:left;font-size:1.25rem}
#sheet .list{padding-bottom:calc(var(--sb) + 20px)}

#net,#toast{position:fixed;left:50%;transform:translateX(-50%);padding:8px 16px;border-radius:999px;font-size:.84rem;
  font-weight:600;pointer-events:none;opacity:0;transition:opacity .25s;white-space:nowrap;z-index:9}
#net{top:calc(var(--st) + 8px);background:rgba(255,170,0,.92);color:#000}
body.off #net{opacity:1}
#toast{bottom:calc(var(--tab) + var(--sb) + var(--mini) + 20px);background:rgba(250,250,250,.94);color:#000}
#toast.on{opacity:1}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>

<div class=bg id=bgA></div><div class=bg id=bgB></div><div id=shade></div>

<section class="view on" id=v-now>
  <div class=artwrap><div id=artbox><img id=art alt=""><svg viewBox="0 0 24 24"><path d="M12 3v10.55A4 4 0 1014 17V7h4V3h-6z"/></svg></div></div>
  <div><div id=t>&mdash;</div><div id=a>&nbsp;</div></div>
  <div id=prog>
    <div class=sl id=pr role=slider aria-label="Position"><div class=tr><div class=fl></div></div></div>
    <div id=times><span id=cur>0:00</span><span id=rem>-0:00</span></div>
  </div>
  <div id=ctrl>
    <button data-i=prev data-cmd=prev aria-label="Previous"></button>
    <button id=pp data-i=play aria-label="Play or pause"></button>
    <button data-i=next data-cmd=next aria-label="Next"></button>
  </div>
  <div id=vol>
    <button id=mute data-i=vol0 data-cmd=mute aria-label="Mute"></button>
    <div class=sl id=vr role=slider aria-label="Volume"><div class=tr><div class=fl></div></div></div>
    <svg viewBox="0 0 24 24" width=20 height=20><path d="M3 9v6h4l6 5V4L7 9H3z"/><path d="M16 8a5.5 5.5 0 010 8l-1.3-1.5a3.5 3.5 0 000-5L16 8z"/><path d="M18.5 5.5a9 9 0 010 13l-1.3-1.5a7 7 0 000-10l1.3-1.5z"/></svg>
  </div>
  <div id=acts>
    <button id=shf data-i=shuf aria-label="Shuffle"></button>
    <button id=rpt data-i=rep aria-label="Repeat"></button>
    <button id=qbtn data-i=queue aria-label="Playing next"></button>
  </div>
</section>

<section class=view id=v-lib>
  <div class=page id=p-pls>
    <div class=head><h1>Library</h1></div>
    <div class="list scroll" id=pls></div>
  </div>
  <div class=page id=p-trs hidden>
    <div class=head>
      <div class=bar><button data-i=back data-back aria-label="Back"></button><div class=bt id=trs-title></div>
        <button id=trs-refresh data-i=refresh aria-label="Reload playlist"></button></div>
      <input class=field id=flt type=search placeholder="Filter" autocomplete=off autocorrect=off spellcheck=false>
    </div>
    <div class="list scroll" id=trs></div>
  </div>
</section>

<section class=view id=v-src>
  <div class=page>
    <div class=head><h1>Search</h1>
      <form id=sf action="#"><input class=field id=sq type=search enterkeyhint=search placeholder="Artists, songs, lyrics"
        autocomplete=off autocorrect=off spellcheck=false></form></div>
    <div class="list scroll" id=src><div class=hint>Search the Apple Music catalog and play any song on your PC.</div></div>
  </div>
</section>

<div id=mini>
  <img id=mart alt=""><div class=rt><b id=mt>&mdash;</b><span id=ma></span></div>
  <button id=mpp data-i=play aria-label="Play or pause"></button>
  <button data-i=next data-cmd=next aria-label="Next"></button>
  <div id=mprog><i></i></div>
</div>
<nav id=tabs>
  <button data-tab=now data-i=note class=on>Now Playing</button>
  <button data-tab=lib data-i=lib>Library</button>
  <button data-tab=src data-i=search>Search</button>
</nav>

<div id=dim></div>
<div id=sheet>
  <div class=grab></div>
  <div class=bar><div class=bt>Playing Next</div><button data-i=down data-back aria-label="Close"></button></div>
  <div class="list scroll" id=que></div>
</div>
<div id=net>Reconnecting&hellip;</div><div id=toast></div>

<script>
'use strict';
const $=i=>document.getElementById(i),B=document.body;
const I={
 play:'M8 5.3v13.4a1 1 0 001.5.86l11-6.7a1 1 0 000-1.72l-11-6.7A1 1 0 008 5.3z',
 pause:'M6.5 5h3.2a.8.8 0 01.8.8v12.4a.8.8 0 01-.8.8H6.5a.8.8 0 01-.8-.8V5.8a.8.8 0 01.8-.8zm7.8 0h3.2a.8.8 0 01.8.8v12.4a.8.8 0 01-.8.8h-3.2a.8.8 0 01-.8-.8V5.8a.8.8 0 01.8-.8z',
 prev:'M20 5v14l-9.5-7L20 5zM10.5 5v14L1 12l9.5-7z',next:'M4 5v14l9.5-7L4 5zM13.5 5v14L23 12l-9.5-7z',
 vol0:'M3 9v6h4l6 5V4L7 9H3z',
 shuf:'M10.59 9.17 5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41-1.42 1.42 3.14 3.14L14.5 20H20v-5.5l-2.04 2.05-3.13-3.14z',
 rep:'M17 17H7v-3l-4 4 4 4v-3h12v-6h-2v4zM7 7h10v3l4-4-4-4v3H5v6h2V7z',
 rep1:'M13 15V9h-1l-2 1v1h1.5v4H13zM17 17H7v-3l-4 4 4 4v-3h12v-6h-2v4zM7 7h10v3l4-4-4-4v3H5v6h2V7z',
 queue:'M3 5h12v2H3zM3 10h12v2H3zM3 15h7v2H3zM16.5 12.5l6 3.5-6 3.5z',
 note:'M12 3v10.55A4 4 0 1014 17V7h4V3h-6z',
 lib:'M3 6h12v2H3zM3 11h12v2H3zM3 16h8v2H3zM19 6v8.3a3.2 3.2 0 101.5 2.7V8h2V6h-3.5z',
 search:'M10 2a8 8 0 105.29 14.03l4.34 4.34 1.41-1.41-4.34-4.34A8 8 0 0010 2zm0 2a6 6 0 110 12 6 6 0 010-12z',
 back:'M15 4l-8 8 8 8 1.6-1.6L10.2 12l6.4-6.4z',down:'M7.4 8.6L12 13.2l4.6-4.6L18 10l-6 6-6-6z',
 chev:'M9 4l8 8-8 8-1.6-1.6L13.8 12 7.4 5.6z',
 refresh:'M17.65 6.35A7.95 7.95 0 0012 4a8 8 0 108 8h-2a6 6 0 11-6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z'};
const svg=(n,c)=>'<svg'+(c?' class='+c:'')+' viewBox="0 0 24 24" aria-hidden="true"><path d="'+I[n]+'"/></svg>';
document.querySelectorAll('[data-i]').forEach(b=>b.insertAdjacentHTML('afterbegin',svg(b.dataset.i)));
const esc=s=>(s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const fmt=x=>{x=Math.max(0,Math.round(x));const h=x/3600|0,m=(x%3600)/60|0,s=String(x%60).padStart(2,'0');
  return h?h+':'+String(m).padStart(2,'0')+':'+s:m+':'+s};
const store={get(k){try{return JSON.parse(localStorage.getItem('amr.'+k))}catch(e){return null}},
  set(k,v){try{localStorage.setItem('amr.'+k,JSON.stringify(v))}catch(e){}}};
const post=(u,b)=>fetch(u,{method:'POST',body:b?JSON.stringify(b):null});
const getj=async u=>{const r=await(await fetch(u)).json();if(!r.ok)throw new Error(r.err||'failed');return r};
const postj=async(u,b)=>{const r=await(await post(u,b)).json();if(!r.ok)throw new Error(r.err||'failed');return r};
const setv=(el,v)=>el.style.setProperty('--v',Math.min(1,Math.max(0,v||0)));
const text=(id,s)=>{const el=$(id);if(el.textContent!==s)el.textContent=s};
let toastT;function toast(m){const el=$('toast');el.textContent=m;el.classList.add('on');clearTimeout(toastT);
  toastT=setTimeout(()=>el.classList.remove('on'),2600)}

/* ---- sliders: pointer-driven, whole strip is the hit area ---- */
function slider(el,onInput,onCommit){let rect,last=0;
  const val=e=>last=Math.min(1,Math.max(0,(e.clientX-rect.left)/rect.width));
  el.addEventListener('pointerdown',e=>{rect=el.getBoundingClientRect();try{el.setPointerCapture(e.pointerId)}catch(x){}
    el.drag=true;el.classList.add('drag');onInput(val(e))});
  el.addEventListener('pointermove',e=>{if(el.drag)onInput(val(e))});
  const end=()=>{if(!el.drag)return;el.drag=false;el.classList.remove('drag');onCommit&&onCommit(last)};
  el.addEventListener('pointerup',end);el.addEventListener('pointercancel',end)}

/* ---- player state ---- */
let pos=0,posAt=0,dur=0,playing=false,seekAt=0,volAt=0,toggleAt=0,modeAt=0,artId,nowKey='',lastCur='',raf=0,lastMsg=0;
let modes={shuffle:false,repeat:'none'};
const curPos=()=>{const p=pos+(playing?(performance.now()-posAt)/1000:0);return dur>0?Math.min(p,dur):p};
function setPlaying(p){playing=p;B.classList.toggle('paused',!p);const h=svg(p?'pause':'play');
  if($('pp').dataset.s!==String(p)){$('pp').dataset.s=String(p);$('pp').innerHTML=h;$('mpp').innerHTML=h}}
function showTime(p){const c=fmt(p);if(c!==lastCur){lastCur=c;$('cur').textContent=c;$('rem').textContent='-'+fmt(dur-p)}}
function frame(){raf=0;const p=curPos(),v=dur>0?p/dur:0;
  if(!$('pr').drag){setv($('pr'),v);showTime(p)}
  setv($('mprog'),v);
  if(playing&&!document.hidden)raf=requestAnimationFrame(frame)}
function kick(){if(!raf)raf=requestAnimationFrame(frame)}

function setArt(id){if(id===artId)return;artId=id;
  if(!id){B.classList.add('noart');return}
  const url='/artwork/'+id,im=new Image();
  im.onload=()=>{if(artId!==id)return;B.classList.remove('noart');$('art').src=url;$('mart').src=url;
    const a=$('bgA'),b=$('bgB'),top=a.classList.contains('on')?b:a,old=top===a?b:a;
    top.style.backgroundImage='url('+url+')';top.classList.add('on');old.classList.remove('on')};
  im.onerror=()=>{if(artId===id)B.classList.add('noart')};im.src=url}

function renderModes(){$('shf').classList.toggle('on',!!modes.shuffle);$('rpt').classList.toggle('on',modes.repeat!=='none');
  const k=modes.repeat==='one'?'rep1':'rep';if($('rpt').dataset.s!==k){$('rpt').dataset.s=k;$('rpt').innerHTML=svg(k)}}

function apply(s){lastMsg=Date.now();
  const title=s.ok?(s.title||'Not Playing'):'Apple Music is not running';
  text('t',title);text('a',s.ok?[s.artist,s.album].filter(Boolean).join(' — ')||' ':' ');
  text('mt',title);text('ma',s.ok?(s.artist||''):'');
  dur=s.ok?(s.dur||0):0;B.classList.toggle('nodur',!(dur>0));
  if(Date.now()-toggleAt>1500)setPlaying(!!(s.ok&&s.playing));
  if(!$('pr').drag&&Date.now()-seekAt>1800){pos=s.pos||0;posAt=performance.now()}
  if(s.vol!=null&&!$('vr').drag&&Date.now()-volAt>1200)setv($('vr'),s.vol/100);
  B.classList.toggle('muted',!!s.muted);
  setArt(s.ok?s.art:null);
  if(s.shuffle!=null&&Date.now()-modeAt>2500){modes={shuffle:s.shuffle,repeat:s.repeat||'none'};renderModes()}
  const k=s.ok?(s.title||'')+'\n'+(s.artist||''):'';if(k!==nowKey){nowKey=k;markPlaying()}
  lastCur='';kick()}

/* ---- live connection: the server pushes every change ---- */
let es;
function connect(){if(es)es.close();es=new EventSource('/events');
  es.onmessage=e=>{B.classList.remove('off');apply(JSON.parse(e.data))};
  es.onerror=()=>B.classList.add('off')}
document.addEventListener('visibilitychange',()=>{if(document.hidden)return;
  if(!es||es.readyState===2||Date.now()-lastMsg>12000)connect();kick()});
setInterval(()=>{if(!document.hidden&&Date.now()-lastMsg>25000)connect()},5000);

/* ---- controls ---- */
function toggle(){pos=curPos();posAt=performance.now();toggleAt=Date.now();setPlaying(!playing);kick();post('/cmd/toggle')}
$('pp').onclick=toggle;$('mpp').onclick=e=>{e.stopPropagation();toggle()};
document.querySelectorAll('[data-cmd]').forEach(b=>b.onclick=e=>{e.stopPropagation();post('/cmd/'+b.dataset.cmd)});
function mode(x){modeAt=Date.now();if(x==='shuffle')modes.shuffle=!modes.shuffle;
  else modes.repeat=modes.repeat==='none'?'all':modes.repeat==='all'?'one':'none';
  renderModes();post('/cmd/'+x)}
$('shf').onclick=()=>mode('shuffle');$('rpt').onclick=()=>mode('repeat');
slider($('pr'),v=>{if(!(dur>0))return;setv($('pr'),v);showTime(v*dur)},
  v=>{if(!(dur>0))return;pos=v*dur;posAt=performance.now();seekAt=Date.now();post('/cmd/seek/'+Math.round(pos));kick()});
/* volume: send the latest value as soon as the previous request returns (no fixed debounce) */
let vNext=null,vBusy=false;
async function pushVol(){if(vBusy)return;vBusy=true;
  while(vNext!=null){const v=vNext;vNext=null;try{await post('/cmd/vol/'+v)}catch(e){}}vBusy=false}
slider($('vr'),v=>{volAt=Date.now();setv($('vr'),v);vNext=Math.round(v*100);pushVol()},()=>{volAt=Date.now()});

/* ---- navigation ---- */
let tab='now',curPl=null,sheetOpen=false,libInit=false;
function go(t){tab=t;['now','lib','src'].forEach(x=>$('v-'+x).classList.toggle('on',x===t));
  B.classList.toggle('sub',t!=='now');
  document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.tab===t));
  if(t==='lib'&&!libInit){libInit=true;renderPls();loadPls()}
  kick()}
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{
  if(b.dataset.tab===tab&&tab==='lib'&&curPl)history.back();else go(b.dataset.tab)});
$('mini').onclick=()=>go('now');
document.querySelectorAll('[data-back]').forEach(b=>b.onclick=()=>history.back());
$('dim').onclick=()=>history.back();
addEventListener('popstate',()=>{if(sheetOpen)closeSheet();else if(curPl)closeTracks()});
const rowOf=(e,list)=>{const r=e.target.closest('.row');return r&&list.contains(r)&&r.dataset.j!=null?r:null};
const sub=t=>[t.a,t.al].filter(Boolean).join(' — ');

/* ---- library: cached on the phone, so it opens instantly ---- */
let pls=store.get('pls'),tracks={},hay=[];
function renderPls(){const el=$('pls');if(!pls){el.innerHTML='<div class=spin></div>';return}
  el.innerHTML=pls.map((p,i)=>'<div class=row data-j='+i+'><div class=badge style="filter:hue-rotate('+(i*47)+'deg)">'
    +esc(Array.from(p.name||'♪')[0])+'</div><div class=rt><b>'+esc(p.name)+'</b></div>'+svg('chev','chev')+'</div>').join('')
    ||'<div class=hint>No playlists found in the Apple Music sidebar.</div>'}
async function loadPls(){try{const r=await getj('/playlists');
    if(JSON.stringify(r.items)!==JSON.stringify(pls)){pls=r.items;store.set('pls',pls);renderPls()}}
  catch(e){if(!pls)$('pls').innerHTML='<div class=hint>Could not load playlists.<br>Is Apple Music running on the PC?</div>'}}
$('pls').onclick=e=>{const r=rowOf(e,$('pls'));if(r)openTracks(pls[+r.dataset.j])};

function openTracks(p){curPl=p;$('p-pls').hidden=true;$('p-trs').hidden=false;text('trs-title',p.name);$('flt').value='';
  history.pushState({v:'trs'},'');loadTracks(false)}
function closeTracks(){curPl=null;$('p-trs').hidden=true;$('p-pls').hidden=false}
async function loadTracks(fresh){const p=curPl,el=$('trs');
  let t=fresh?null:(tracks[p.id]||store.get('tr.'+p.id));
  if(t){tracks[p.id]=t;renderTracks();return}
  el.innerHTML='<div class=spin></div><div class=hint>Reading the playlist from Apple Music&hellip;</div>';
  $('trs-refresh').classList.add('spinning');
  try{const r=await getj('/tracks?pl='+encodeURIComponent(p.id)+(fresh?'&fresh=1':''));
    tracks[p.id]=r.items;store.set('tr.'+p.id,r.items);if(curPl===p)renderTracks()}
  catch(e){if(curPl===p)el.innerHTML='<div class=hint>Could not load this playlist.</div>'}
  $('trs-refresh').classList.remove('spinning')}
function renderTracks(){const t=tracks[curPl.id]||[];hay=t.map(x=>(x.t+' '+x.a+' '+(x.al||'')).toLowerCase());
  $('trs').innerHTML=t.length?'<div class=count>'+t.length+' song'+(t.length===1?'':'s')+'</div>'
    +t.map((x,j)=>'<div class=row data-j='+j+'><div class=rt><b>'+esc(x.t)+'</b><span>'+esc(sub(x))+'</span></div>'
    +'<div class=dur>'+esc(x.d)+'</div></div>').join(''):'<div class=hint>No songs in this playlist.</div>';
  $('trs').scrollTop=0;markPlaying();filter()}
function filter(){const q=$('flt').value.trim().toLowerCase();
  $('trs').querySelectorAll('.row').forEach(r=>{r.hidden=!!q&&!hay[+r.dataset.j].includes(q)})}
$('flt').oninput=filter;
$('trs-refresh').onclick=()=>{if(curPl&&!$('trs-refresh').classList.contains('spinning'))loadTracks(true)};
function markPlaying(){if(!curPl)return;const t=tracks[curPl.id]||[],title=nowKey.split('\n')[0];
  $('trs').querySelectorAll('.row').forEach(r=>r.classList.toggle('playing',!!title&&t[+r.dataset.j].t===title))}
async function play(row,url,body){row.classList.add('busy');
  try{await postj(url,body)}catch(e){toast('Could not play that song')}
  row.classList.remove('busy')}
$('trs').onclick=e=>{const r=rowOf(e,$('trs'));
  if(r)play(r,'/play',{pl:curPl.id,id:tracks[curPl.id][+r.dataset.j].id})};

/* ---- search ---- */
let srcItems=[],lastQ='';
$('sf').onsubmit=async e=>{e.preventDefault();const q=$('sq').value.trim();if(!q)return;lastQ=q;$('sq').blur();
  const el=$('src');el.innerHTML='<div class=spin></div>';
  try{const r=await getj('/search?q='+encodeURIComponent(q));if(lastQ!==q)return;srcItems=r.items;
    el.innerHTML=srcItems.map((t,j)=>'<div class=row data-j='+j+'><div class=rt><b>'+esc(t.t)+'</b><span>'+esc(t.a)+'</span></div></div>').join('')
      ||'<div class=hint>No songs found.</div>';el.scrollTop=0}
  catch(x){el.innerHTML='<div class=hint>Search failed. Is Apple Music running on the PC?</div>'}};
$('src').onclick=e=>{const r=rowOf(e,$('src'));
  if(r)play(r,'/splay',{q:lastQ,i:+r.dataset.j,id:srcItems[+r.dataset.j].id})};

/* ---- queue sheet ---- */
function closeSheet(){sheetOpen=false;B.classList.remove('sheet')}
$('qbtn').onclick=async()=>{sheetOpen=true;B.classList.add('sheet');history.pushState({v:'que'},'');
  const el=$('que');el.innerHTML='<div class=spin></div>';
  try{const r=await getj('/queue');
    el.innerHTML=r.items.map(t=>'<div class="row static"><div class=rt><b>'+esc(t.t)+'</b><span>'+esc(t.a)+'</span></div>'
      +'<div class=dur>'+esc(t.d)+'</div></div>').join('')||'<div class=hint>Nothing is queued.</div>'}
  catch(e){el.innerHTML='<div class=hint>Could not read the queue.</div>'}};

B.classList.add('paused','noart','nodur');setPlaying(false);renderModes();connect();
</script>"""


# --------------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------------

class Hub:
    """The latest player state, plus change notification for SSE clients.

    Workers publish into it; HTTP threads only ever read a ready snapshot.
    The playback position is stored as (seconds, monotonic timestamp) and
    extrapolated on read, so a steadily advancing position is not a change.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._state: dict = {"ok": False}
        self._pos = (0.0, time.monotonic())
        self._hold_until = 0.0
        self._version = 0
        self._listeners = 0
        self._last_seen = 0.0

    def _position(self, now: float) -> float:
        seconds, at = self._pos
        if self._state.get("playing"):
            seconds += now - at
        duration = self._state.get("dur") or 0
        return max(0.0, min(seconds, duration) if duration > 0 else seconds)

    def update(self, changes: Optional[dict] = None,
               position: Optional[tuple] = None, hold: float = 0.0) -> None:
        """Merge ``changes``; ``position`` is (seconds, monotonic time measured).

        ``hold`` pins a position we set ourselves (a seek) for that many
        seconds, so a stale timeline report cannot yank the scrubber back.
        """
        with self._cond:
            now = time.monotonic()
            before = self._position(now)
            changed = False
            if changes:
                diff = {k: v for k, v in changes.items() if self._state.get(k) != v}
                if diff:
                    self._state = {**self._state, **diff}
                    changed = True
            if position is not None:
                previous = self._pos
                self._pos = position
                if hold:
                    self._hold_until = now + hold
                elif now < self._hold_until and abs(self._position(now) - before) > 2.0:
                    self._pos = previous
            if abs(self._position(now) - before) > 1.0:
                changed = True
            if changed:
                self._version += 1
                self._cond.notify_all()

    def _snapshot(self) -> dict:
        return {**self._state, "pos": round(self._position(time.monotonic()), 2)}

    def snapshot(self) -> dict:
        with self._cond:
            self._last_seen = time.monotonic()
            return self._snapshot()

    def wait(self, version: int, timeout: float) -> tuple:
        """Block until the state differs from ``version`` (or ``timeout``)."""
        with self._cond:
            self._cond.wait_for(lambda: self._version != version, timeout)
            return self._version, self._snapshot()

    def listen(self, delta: int) -> None:
        with self._cond:
            self._listeners += delta

    def active(self) -> bool:
        """True while a phone is connected; workers idle otherwise."""
        return self._listeners > 0 or time.monotonic() - self._last_seen < 10


HUB = Hub()


# --------------------------------------------------------------------------
# System volume (pycaw / Core Audio)
# --------------------------------------------------------------------------

class Audio(threading.Thread):
    """Owns the Core Audio endpoint on a single COM-initialized thread."""

    def __init__(self) -> None:
        super().__init__(name="audio", daemon=True)
        self._commands: queue.Queue = queue.Queue()

    def post(self, *command) -> None:
        self._commands.put(command)

    def run(self) -> None:
        CoInitialize()
        device, opened = None, 0.0
        while True:
            try:
                commands = [self._commands.get(timeout=0.5 if HUB.active() else 5)]
            except queue.Empty:
                commands = []
            while True:  # drain, so a slider drag collapses to its latest value
                try:
                    commands.append(self._commands.get_nowait())
                except queue.Empty:
                    break
            try:
                # Re-resolve periodically so a changed default device is followed.
                if device is None or time.monotonic() - opened > 10:
                    device, opened = AudioUtilities.GetSpeakers(), time.monotonic()
                endpoint = device.EndpointVolume
                volumes = [c[1] for c in commands if c[0] == "vol"]
                if volumes:
                    self._set(device, endpoint, volumes[-1])
                if sum(1 for c in commands if c[0] == "mute") % 2:
                    endpoint.SetMute(not endpoint.GetMute(), None)
                # pycaw exposes volume_percent (PR #109) on newer releases; fall
                # back to the scalar API on older ones.
                percent = (device.volume_percent if hasattr(device, "volume_percent")
                           else endpoint.GetMasterVolumeLevelScalar() * 100)
                HUB.update({"vol": round(percent), "muted": bool(endpoint.GetMute())})
            except Exception:
                device = None
                time.sleep(0.5)

    @staticmethod
    def _set(device, endpoint, percent: int) -> None:
        percent = max(0, min(100, percent))
        if hasattr(device, "volume_percent"):
            device.volume_percent = percent
        else:
            endpoint.SetMasterVolumeLevelScalar(percent / 100, None)


# --------------------------------------------------------------------------
# Playback state and transport (SMTC / winrt)
# --------------------------------------------------------------------------

def _image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"GIF8"):
        return "image/gif"
    return "application/octet-stream"


class Smtc(threading.Thread):
    """Tracks the media session on one persistent asyncio loop.

    Windows raises events when the track, playback state or timeline change,
    so the state is refreshed immediately; a slow timer is only a safety net.
    This thread must never be COM-initialized by comtypes: awaiting WinRT
    stream operations on such a thread never completes.
    """

    def __init__(self) -> None:
        super().__init__(name="smtc", daemon=True)
        self.artwork: Optional[tuple] = None  # (id, bytes, content type)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._session = None
        self._session_id = None
        self._tokens: list = []
        self._repick = True
        self._props_dirty = True
        self._track = None
        self._art_tries = 0
        self._was_playing: Optional[bool] = None  # None until first observed
        self._playing_since: Optional[float] = None

    def run(self) -> None:
        asyncio.run(self._main())

    # -- called from other threads ---------------------------------------

    def poke(self, repick: bool = False, props: bool = False) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._on_poke, repick, props)

    def command(self, name: str) -> None:
        self._ready.wait(5)
        asyncio.run_coroutine_threadsafe(self._command(name), self._loop).result(5)

    # -- loop thread ------------------------------------------------------

    def _on_poke(self, repick: bool, props: bool) -> None:
        self._repick = self._repick or repick
        self._props_dirty = self._props_dirty or props
        self._wake.set()

    async def _command(self, name: str) -> None:
        session = self._session
        if session is None:
            return
        await {"toggle": session.try_toggle_play_pause_async,
               "next": session.try_skip_next_async,
               "prev": session.try_skip_previous_async}[name]()
        self._loop.call_later(0.2, self._wake.set)

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._ready.set()
        manager, ticks = None, 0
        while True:
            try:
                if manager is None:
                    manager = await SessionManager.request_async()
                    manager.add_sessions_changed(lambda *_: self.poke(repick=True))
                    manager.add_current_session_changed(lambda *_: self.poke(repick=True))
                    self._repick = True
                ticks += 1
                if self._repick or self._session is None or ticks % 5 == 0:
                    self._pick(manager)
                await self._refresh()
            except Exception:
                manager = None
                self._drop_session()
                HUB.update({"ok": False})
                await asyncio.sleep(1)
            try:
                await asyncio.wait_for(self._wake.wait(), 1.0 if HUB.active() else 5.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def _drop_session(self) -> None:
        session, tokens = self._session, self._tokens
        self._session, self._session_id, self._tokens = None, None, []
        self._was_playing = self._playing_since = None
        if session is not None and tokens:
            for remove, token in zip((session.remove_media_properties_changed,
                                      session.remove_playback_info_changed,
                                      session.remove_timeline_properties_changed), tokens):
                try:
                    remove(token)
                except Exception:
                    pass

    def _pick(self, manager) -> None:
        forced, self._repick = self._repick, False
        chosen = None
        for session in manager.get_sessions():
            if "applemusic" in (session.source_app_user_model_id or "").lower():
                chosen = session
                break
        # Fall back to whatever is currently playing so the transport controls
        # still work if Apple Music has no active session.
        chosen = chosen or manager.get_current_session()
        chosen_id = chosen.source_app_user_model_id if chosen else None
        if not forced and self._session is not None and chosen_id == self._session_id:
            return
        self._drop_session()
        if chosen is None:
            return
        self._session, self._session_id = chosen, chosen_id
        self._tokens = [
            chosen.add_media_properties_changed(lambda *_: self.poke(props=True)),
            chosen.add_playback_info_changed(lambda *_: self.poke()),
            chosen.add_timeline_properties_changed(lambda *_: self.poke()),
        ]
        self._props_dirty = True

    async def _refresh(self) -> None:
        session = self._session
        if session is None:
            self.artwork = None
            HUB.update({"ok": False, "title": "", "artist": "", "album": "",
                        "playing": False, "dur": 0, "art": None}, position=(0.0, time.monotonic()))
            return
        props = await session.try_get_media_properties_async()
        playing = session.get_playback_info().playback_status == PlaybackStatus.PLAYING
        timeline = session.get_timeline_properties()

        title = props.title or ""
        # Apple Music reports "Artist — Album" in the artist field.
        artist, _, album = (props.artist or "").partition(" — ")
        album = album or props.album_title or ""

        track = (title, props.artist)
        if track != self._track:
            self._track, self._art_tries = track, 3
        if self._props_dirty or self._art_tries > 0:
            # The thumbnail often lands a moment after the title; an event
            # normally announces it, the retries cover the case it doesn't.
            self._props_dirty = False
            self._art_tries = max(0, self._art_tries - 1)
            await self._load_artwork(props, final=self._art_tries == 0)

        now = time.monotonic()
        if playing and self._was_playing is False:
            self._playing_since = now  # we saw it resume
        elif not playing or self._was_playing is None:
            self._playing_since = None  # paused, or already playing when we looked
        self._was_playing = playing

        seconds = timeline.position.total_seconds()
        age = 0.0
        if playing:
            try:
                age = (datetime.now(timezone.utc) - timeline.last_updated_time).total_seconds()
            except Exception:
                age = 0.0
            if not 0 <= age <= 86400:
                age = 0.0
            # Right after a resume the app has not re-stamped its timeline yet:
            # the stamp is as old as the pause, and trusting it would throw the
            # position far ahead. Playback cannot have run longer than we have
            # seen it running.
            if self._playing_since is not None:
                age = min(age, now - self._playing_since)
        HUB.update({"ok": True, "title": title, "artist": artist, "album": album,
                    "playing": playing, "dur": round(timeline.end_time.total_seconds(), 2),
                    "art": self.artwork[0] if self.artwork else None},
                   position=(seconds, time.monotonic() - age))

    async def _load_artwork(self, props, final: bool) -> None:
        data = None
        try:
            if props.thumbnail:
                stream = await props.thumbnail.open_read_async()
                try:
                    if stream.size:
                        buffer = Buffer(stream.size)
                        await stream.read_async(buffer, stream.size, InputStreamOptions.READ_AHEAD)
                        data = bytes(memoryview(buffer)[:buffer.length])
                finally:
                    stream.close()
        except OSError:  # the thumbnail stream can vanish during track changes
            data = None
        if data:
            art_id = hashlib.blake2s(data, digest_size=6).hexdigest()
            if self.artwork is None or self.artwork[0] != art_id:
                self.artwork = (art_id, data, _image_type(data))
        elif final:
            self.artwork = None


# --------------------------------------------------------------------------
# UI Automation bridge (seek, shuffle, repeat, playlists, search, queue)
#
# Apple Music's SMTC integration rejects position changes and does not expose
# shuffle/repeat or any library access, so these features drive the app's own
# window through UI Automation patterns (no mouse/keyboard simulation).
#
# Two things keep this fast:
#   * searches run inside the UIA core (FindFirst/FindAll with a condition)
#     instead of walking the tree from Python one cross-process call at a time;
#   * list contents are read with a cache request, so a whole screen of rows
#     and their text arrives in a single cross-process call.
# All UIA work runs on one worker thread, which also serializes it.
# --------------------------------------------------------------------------

_PROP = uiautomation.PropertyId
_TYPE = uiautomation.ControlType
_CHILDREN, _DESCENDANTS, _SUBTREE = 2, 4, 7
_user32 = ctypes.windll.user32
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _find_apple_music_hwnd() -> Optional[int]:
    """Locate the Apple Music top-level window without touching UIA.

    Matching on the window class as well as the title keeps a browser tab
    that merely has "Apple Music" in its title from being picked.
    """
    exact, loose = [], []
    text = ctypes.create_unicode_buffer(256)

    def visit(hwnd, _):
        if _user32.IsWindowVisible(hwnd):
            _user32.GetWindowTextW(hwnd, text, 256)
            title = text.value
            if APPLE_MUSIC_WINDOW in title:
                _user32.GetClassNameW(hwnd, text, 256)
                if text.value == APPLE_MUSIC_WINDOW_CLASS:
                    exact.append(hwnd)
                elif title == APPLE_MUSIC_WINDOW:
                    loose.append(hwnd)
        return True

    _user32.EnumWindows(_ENUM_WINDOWS_PROC(visit), 0)
    found = exact or loose
    return found[0] if found else None


class Row(NamedTuple):
    name: str     # accessible name; unique enough to address the row later
    top: object   # screen y while inside the viewport, else None
    texts: list   # visible text cells: title, artist, album, ..., duration
    element: object


class Uia(threading.Thread):
    def __init__(self) -> None:
        super().__init__(name="uia", daemon=True)
        self._jobs: queue.Queue = queue.Queue()
        self._hwnd: Optional[int] = None
        self._scrubber = None
        self._navigated = 0.0                     # when a playlist page was last opened
        self.playlists: list[dict] = []           # [{"id": <AutomationId>, "name": <str>}]
        self.tracks: dict[str, list[dict]] = {}   # playlist id -> parsed tracks
        self._marks: dict[str, dict] = {}         # playlist id -> {row id: scroll percent}
        self._search: Optional[tuple] = None      # (query, items) of the last search

    # -- job plumbing -----------------------------------------------------

    def call(self, fn, *args, timeout: float = 180):
        """Run ``fn`` on the UIA thread and wait for its result."""
        future: Future = Future()
        self._jobs.put((fn, args, future))
        return future.result(timeout)

    def post(self, fn, *args) -> None:
        self._jobs.put((fn, args, None))

    def run(self) -> None:
        with uiautomation.UIAutomationInitializerInThread():
            self._iuia = _uia._AutomationClient.instance().IUIAutomation
            request = self._iuia.CreateCacheRequest()
            for prop in (_PROP.NameProperty, _PROP.ControlTypeProperty,
                         _PROP.BoundingRectangleProperty):
                request.AddProperty(prop)
            request.TreeScope = _SUBTREE
            self._subtree_request = request
            # Searches cover the raw view: some of the app's controls (the search
            # button, for one) are not part of the default control view.
            self._raw_request = self._iuia.CreateCacheRequest()
            self._raw_request.TreeFilter = self._iuia.RawViewCondition
            while True:
                try:
                    fn, args, future = self._jobs.get(timeout=4)
                except queue.Empty:
                    # Keep shuffle/repeat in sync with changes made on the PC.
                    if HUB.active():
                        try:
                            self.refresh_modes()
                        except Exception:
                            pass
                    continue
                try:
                    result = fn(*args)
                except Exception as exc:
                    if future is not None:
                        future.set_exception(exc)
                else:
                    if future is not None:
                        future.set_result(result)

    # -- element helpers --------------------------------------------------

    def _window(self):
        if not self._hwnd or not _user32.IsWindow(self._hwnd):
            self._hwnd, self._scrubber = _find_apple_music_hwnd(), None
        if not self._hwnd:
            raise RuntimeError("Apple Music window not found")
        return self._iuia.ElementFromHandle(self._hwnd)

    def _condition(self, aid=None, name=None, ctype=None):
        parts = [self._iuia.CreatePropertyCondition(prop, value)
                 for prop, value in ((_PROP.AutomationIdProperty, aid),
                                     (_PROP.NameProperty, name),
                                     (_PROP.ControlTypeProperty, ctype))
                 if value is not None]
        condition = parts[0]
        for part in parts[1:]:
            condition = self._iuia.CreateAndCondition(condition, part)
        return condition

    def _find(self, root, scope=_DESCENDANTS, **criteria):
        """First matching element, or None. The search runs inside UIA."""
        return root.FindFirstBuildCache(scope, self._condition(**criteria), self._raw_request) or None

    def _find_all(self, root, scope=_DESCENDANTS, **criteria) -> list:
        found = root.FindAllBuildCache(scope, self._condition(**criteria), self._raw_request)
        return [found.GetElement(i) for i in range(found.Length)] if found else []

    def _wait(self, probe, timeout: float, interval: float = 0.08):
        """Poll ``probe`` until it returns something truthy."""
        deadline = time.monotonic() + timeout
        while True:
            result = probe()
            if result or time.monotonic() >= deadline:
                return result
            time.sleep(interval)

    @staticmethod
    def _control(element):
        return uiautomation.Control.CreateControlFromElement(element)

    def _snapshot(self, element):
        """Re-read ``element`` with its whole subtree in one cross-process call."""
        return element.BuildUpdatedCache(self._subtree_request)

    @staticmethod
    def _kids(element) -> list:
        kids = element.GetCachedChildren()
        return [kids.GetElement(i) for i in range(kids.Length)] if kids else []

    def _texts(self, element, depth: int = 2) -> list:
        """Non-empty text cells directly inside a (cached) row."""
        kids = self._kids(element)
        # Icon glyphs (the "explicit" badge, for one) are text too, drawn from
        # the Unicode private-use areas; they are not part of the metadata.
        texts = [k.CachedName for k in kids
                 if k.CachedControlType == _TYPE.TextControl and k.CachedName
                 and not all(ord(c) >= 0xF0000 or 0xE000 <= ord(c) <= 0xF8FF for c in k.CachedName)]
        if not texts and depth > 1:  # some rows wrap their cells in a grid
            for kid in kids:
                if kid.CachedControlType != _TYPE.ButtonControl:
                    texts = self._texts(kid, depth - 1)
                    if texts:
                        break
        return texts

    def _rows(self, container) -> list:
        """List items (with their text) currently realized under ``container``,
        in list order."""
        rows: list = []
        for _ in range(4):
            box = container.CurrentBoundingRectangle
            rows, pending = [], False
            for element in self._kids(self._snapshot(container)):
                if element.CachedControlType == _TYPE.ListItemControl and element.CachedName:
                    texts = self._texts(element)
                    if not texts:
                        pending = True  # row exists but its cells are not there yet
                        continue
                    # Realized rows outside the viewport report an empty rectangle.
                    rect = element.CachedBoundingRectangle
                    visible = rect.bottom > rect.top and box.top <= rect.top < box.bottom
                    rows.append(Row(element.CachedName, rect.top if visible else None,
                                    texts, element))
            if not pending:
                break
            time.sleep(0.1)
        return rows
    def _settled_rows(self, container, timeout: float = 1.5) -> list:
        """Read rows until two consecutive reads agree (content still loading)."""
        deadline, rows = time.monotonic() + timeout, self._rows(container)
        while time.monotonic() < deadline:
            time.sleep(0.15)
            again = self._rows(container)
            if len(again) == len(rows):
                return again
            rows = again
        return rows

    @staticmethod
    def _track(row: Row) -> dict:
        texts = row.texts
        duration = texts[-1] if TRACK_DURATION_RE.match(texts[-1]) else ""
        middle = texts[1:-1] if duration else texts[1:]
        return {"t": texts[0], "a": middle[0] if middle else "",
                "al": middle[1] if len(middle) > 1 else "", "d": duration, "id": row.name}

    # -- transport bar ----------------------------------------------------

    def seek(self, seconds: int) -> None:
        """Seek by setting the transport bar's scrubber slider directly."""
        for attempt in range(2):
            if self._scrubber is None:
                self._scrubber = self._find(self._window(), aid=SCRUBBER_AUTOMATION_ID)
                if self._scrubber is None:
                    raise RuntimeError("scrubber not found")
            try:
                self._control(self._scrubber).GetRangeValuePattern().SetValue(seconds, waitTime=0)
                return
            except Exception:
                self._scrubber = None  # stale element; look it up again once
                if attempt:
                    raise

    def _button(self, window, automation_id: str):
        button = self._find(window, aid=automation_id)
        if button is None:
            raise RuntimeError(automation_id + " not found")
        return button

    def toggle_shuffle(self) -> None:
        self._control(self._button(self._window(), "ShuffleButton")).GetTogglePattern().Toggle(waitTime=0)

    def cycle_repeat(self) -> None:
        """Advance repeat mode: off -> repeat all -> repeat one -> off."""
        button = self._button(self._window(), "RepeatButton")
        self._control(button).GetLegacyIAccessiblePattern().DoDefaultAction(waitTime=0)

    def refresh_modes(self) -> dict:
        window = self._window()
        shuffle = self._control(self._button(window, "ShuffleButton")).GetTogglePattern().ToggleState == 1
        repeat = REPEAT_STATES.get(self._button(window, "RepeatButton").CurrentName, "none")
        HUB.update({"shuffle": shuffle, "repeat": repeat})
        return {"ok": True, "shuffle": shuffle, "repeat": repeat}

    # -- playlists --------------------------------------------------------

    def load_playlists(self) -> dict:
        """Read user playlists from the sidebar. Requires the sidebar's
        'Playlists' section to be expanded (it is by default)."""
        request = self._iuia.CreateCacheRequest()
        request.AddProperty(_PROP.AutomationIdProperty)
        request.AddProperty(_PROP.NameProperty)
        found = self._window().FindAllBuildCache(
            _DESCENDANTS, self._condition(ctype=_TYPE.ListItemControl), request)
        playlists = []
        for i in range(found.Length if found else 0):
            element = found.GetElement(i)
            if "IKIND:ePlaylist" in (element.CachedAutomationId or ""):
                playlists.append({"id": element.CachedAutomationId, "name": element.CachedName})
        self.playlists = playlists
        return {"ok": True, "items": playlists}

    def _track_list(self, window, playlist_name: str):
        """The track ListControl whose header row belongs to ``playlist_name``."""
        for candidate in self._find_all(window, ctype=_TYPE.ListControl):
            if (candidate.CurrentAutomationId or "").startswith(QUEUE_LIST_AUTOMATION_ID):
                continue
            header = candidate.FindFirst(_CHILDREN, self._iuia.CreateTrueCondition())
            if header and (header.CurrentName or "").startswith(playlist_name):
                return candidate
        return None

    def _open_playlist(self, window, playlist_id: str):
        """Show a playlist in the app and return its track ListControl."""
        playlist = next((p for p in self.playlists if p["id"] == playlist_id), None)
        if playlist is None:
            self.load_playlists()
            playlist = next((p for p in self.playlists if p["id"] == playlist_id), None)
            if playlist is None:
                raise RuntimeError("playlist not found")
        item = self._find(window, aid=playlist_id)
        if item is None:
            raise RuntimeError("playlist not found in the sidebar")
        name = playlist["name"]
        if item.GetCurrentPropertyValue(_PROP.SelectionItemIsSelectedProperty):
            track_list = self._track_list(window, name)
            if track_list is not None:  # already on screen: nothing to navigate
                return track_list
        self._control(item).GetSelectionItemPattern().Select(waitTime=0)
        self._navigated = time.monotonic()
        # The previous playlist's list can still be on screen for a moment
        # (and could share a name prefix), so let the page turn over first.
        time.sleep(0.25)
        track_list = self._wait(lambda: self._track_list(window, name), 8)
        if track_list is None:
            raise RuntimeError("track list did not load")
        return track_list

    def _scroller(self, track_list):
        try:
            scroll = self._control(track_list).GetScrollPattern()
            return scroll if scroll and scroll.VerticallyScrollable else None
        except Exception:
            return None

    def _scroll_to(self, scroll, percent: float) -> None:
        # waitTime=0 throughout this class: the library otherwise sleeps half
        # a second after every pattern call. We wait for the actual effect.
        if abs(scroll.VerticalScrollPercent - percent) < 0.3:
            return
        scroll.SetScrollPercent(-1, percent, waitTime=0)
        deadline, last = time.monotonic() + 0.6, None
        while time.monotonic() < deadline:  # wait for the list to actually move
            time.sleep(0.03)
            current = scroll.VerticalScrollPercent
            if abs(current - percent) < 0.6 or current == last:
                break
            last = current
        time.sleep(0.1)  # and for the newly exposed rows to be realized

    def _next_stop(self, scroll, track_list, rows: list, percent: float) -> float:
        """The furthest scroll position that cannot skip a row.

        The list is virtualized: only the viewport plus a look-ahead buffer
        exists in the UIA tree. So instead of a fixed step, jump exactly far
        enough to put the last realized row near the top of the viewport
        (keeping two rows of overlap).
        """
        box = track_list.CurrentBoundingRectangle
        viewport = box.bottom - box.top
        span = viewport * 100.0 / (scroll.VerticalViewSize or 100.0) - viewport  # scrollable pixels
        shown = [i for i, row in enumerate(rows) if row.top is not None]
        if span <= 0 or viewport <= 0:
            return 100.0
        if len(shown) < 2:
            return min(percent + viewport * 0.5 / span * 100.0, 100.0)
        first = shown[0]
        pitch = (rows[shown[-1]].top - rows[first].top) / (len(shown) - 1)
        ahead = (rows[first].top - box.top) + (len(rows) - 1 - first - 2) * pitch
        return min(percent + max(ahead, viewport * 0.5) / span * 100.0, 100.0)

    def _collect_tracks(self, track_list) -> tuple:
        """Read every track of a virtualized list by scrolling through it."""
        scroll = self._scroller(track_list)
        if scroll is not None:
            self._scroll_to(scroll, 0)
            # A freshly opened page creates its look-ahead rows over the next
            # second or so. Reading mid-way works, but waiting briefly lets
            # the first jump cover several screens instead of one.
            time.sleep(max(0.0, 0.9 - (time.monotonic() - self._navigated)))
        seen: set = set()
        tracks: list = []
        marks: dict = {}
        percent, previous = 0.0, 0.0
        while True:
            rows = self._rows(track_list)
            fresh = [row for row in rows if row.name not in seen]
            if scroll is not None and percent > 0 and not rows:
                time.sleep(0.2)  # rows not realized yet: give the list one more beat
                continue
            if tracks and fresh and len(fresh) == len(rows) and percent - previous > 0.5:
                # Nothing overlaps what we already have, so rows in between
                # may have been skipped. Go back half the jump and re-read.
                percent = previous + (percent - previous) / 2
                self._scroll_to(scroll, percent)
                continue
            for row in fresh:
                seen.add(row.name)
                tracks.append(self._track(row))
                marks[row.name] = percent
            if scroll is None or percent >= 100:
                break
            previous = percent
            percent = self._next_stop(scroll, track_list, rows, percent)
            self._scroll_to(scroll, percent)
        if scroll is not None and percent > 0:
            scroll.SetScrollPercent(-1, 0, waitTime=0)  # leave the PC's list where it started
        return tracks, marks

    def load_tracks(self, playlist_id: str, fresh: bool = False) -> dict:
        if fresh or playlist_id not in self.tracks:
            window = self._window()
            track_list = self._open_playlist(window, playlist_id)
            self.tracks[playlist_id], self._marks[playlist_id] = self._collect_tracks(track_list)
        return {"ok": True, "items": self.tracks[playlist_id]}

    def _invoke_row_play(self, window, row) -> None:
        """Play a song row: open its "More" (…) menu and invoke 'Play <title>'."""
        try:
            self._control(row).GetScrollItemPattern().ScrollIntoView(waitTime=0)
            time.sleep(0.15)
        except Exception:
            pass
        buttons = self._find_all(row, ctype=_TYPE.ButtonControl)
        if not buttons:
            raise RuntimeError("row has no menu button")
        title_cell = self._find(row, ctype=_TYPE.TextControl)
        title = (title_cell.CurrentName if title_cell else "") or ""
        self._control(buttons[-1]).GetInvokePattern().Invoke(waitTime=0)

        def play_item():
            for item in self._find_all(window, ctype=_TYPE.MenuItemControl):
                name = item.CurrentName or ""
                # Match 'Play "<title>"' by title so it works in any locale;
                # the quoted form is the English fallback for very short titles.
                if (title and title[:10] in name) or name.startswith("Play “"):
                    return item
            return None

        item = self._wait(play_item, 3, interval=0.05)
        if item is None:
            raise RuntimeError("Play menu item not found")
        self._control(item).GetInvokePattern().Invoke(waitTime=0)

    def play_track(self, playlist_id: str, row_id: str) -> dict:
        window = self._window()
        track_list = self._open_playlist(window, playlist_id)

        def locate():
            return self._find(track_list, _CHILDREN, name=row_id)

        row = locate()
        scroll = None if row is not None else self._scroller(track_list)
        if row is None and scroll is not None:
            # Jump straight to where the row was when the playlist was read.
            mark = self._marks.get(playlist_id, {}).get(row_id)
            if mark is not None:
                self._scroll_to(scroll, mark)
                row = self._wait(locate, 0.6)
        if row is None and scroll is not None:  # playlist changed: scan for it
            self._scroll_to(scroll, 0)
            percent = 0.0
            while row is None:
                row = self._wait(locate, 0.3)
                if row is not None or percent >= 100:
                    break
                percent = self._next_stop(scroll, track_list, self._rows(track_list), percent)
                self._scroll_to(scroll, percent)
        if row is None:
            raise RuntimeError("track not found in playlist")
        self._invoke_row_play(window, row)
        return {"ok": True}

    # -- search -----------------------------------------------------------

    def _search_section(self, window, name: str):
        """Find a named section ('Songs', 'Top Results', ...) on the results page."""
        content = self._find(window, ctype=_TYPE.GroupControl, name="Content")
        return self._find(content, ctype=_TYPE.ListItemControl, name=name) if content else None

    def _search_rows(self, section) -> list:
        snapshot = self._snapshot(section)
        rows, pending = [], [snapshot]
        while pending:  # rows sit a few levels below the section header
            for element in self._kids(pending.pop()):
                if (element.CachedControlType == _TYPE.ListItemControl
                        and element.CachedName and element.CachedName != snapshot.CachedName):
                    texts = self._texts(element)
                    if texts:
                        rows.append(Row(element.CachedName, None, texts, element))
                else:
                    pending.append(element)
        return rows

    def _do_search(self, window, query: str):
        """Type into the sidebar search box, run the search, return the Songs
        section (or None when the results contain no songs)."""
        box = self._find(window, aid="TextBox", ctype=_TYPE.EditControl)
        if box is None:
            raise RuntimeError("search box not found")
        stale = self._search_section(window, "Songs")
        stale_id = stale.GetRuntimeId() if stale else None
        self._control(box).GetValuePattern().SetValue(query, waitTime=0)
        self._control(self._button(window, "QueryButton")).GetInvokePattern().Invoke(waitTime=0)
        started = time.monotonic()
        top_results_since = None
        while time.monotonic() - started < 10:
            time.sleep(0.1)
            elapsed = time.monotonic() - started
            songs = self._search_section(window, "Songs")
            if songs is not None:
                # A results page from the previous search may still be showing;
                # only trust it once it has been replaced (or clearly isn't going to be).
                if stale_id is None or songs.GetRuntimeId() != stale_id or elapsed > 1.6:
                    return songs
            elif self._search_section(window, "Top Results") is not None and (stale_id is None or elapsed > 1.0):
                # Sections render one by one; give Songs a moment to follow.
                top_results_since = top_results_since or time.monotonic()
                if time.monotonic() - top_results_since > 0.7:
                    return None
        raise RuntimeError("search results did not load")

    def search(self, query: str) -> dict:
        window = self._window()
        section = self._do_search(window, query)
        rows = []
        if section is not None:
            # Rows keep arriving after the section appears; read until stable.
            rows = self._search_rows(section)
            for _ in range(8):
                time.sleep(0.15)
                again = self._search_rows(section)
                if len(again) == len(rows) and rows:
                    break
                rows = again
        items = [{"t": row.texts[0], "a": row.texts[1] if len(row.texts) > 1 else "",
                  "id": row.name} for row in rows]
        self._search = (query, items)
        return {"ok": True, "items": items}

    @staticmethod
    def _pick_row(rows: list, index: int, row_id: str):
        """Row names are not unique across results, so match by position
        first and by name as a fallback."""
        if 0 <= index < len(rows) and rows[index].name == row_id:
            return rows[index]
        return next((row for row in rows if row.name == row_id), None)

    def search_play(self, query: str, index: int, row_id: str) -> dict:
        window = self._window()
        row = None
        if self._search and self._search[0] == query:
            # The results page is normally still on screen: play from it directly.
            section = self._search_section(window, "Songs")
            row = self._pick_row(self._search_rows(section), index, row_id) if section else None
        if row is None:  # the app has navigated away; run the search again
            section = self._do_search(window, query)
            rows = self._search_rows(section) if section else []
            for _ in range(8):
                row = self._pick_row(rows, index, row_id)
                if row is not None:
                    break
                time.sleep(0.15)
                rows = self._search_rows(section) if section else []
        if row is None:
            raise RuntimeError("search result not found")
        self._invoke_row_play(window, row.element)
        return {"ok": True}

    # -- up next ----------------------------------------------------------

    def queue(self) -> dict:
        """Read the Up Next panel. Opens it briefly if it isn't already open."""
        window = self._window()
        button = self._control(self._button(window, "PlayQueueToggleButton"))
        toggle = button.GetTogglePattern() if button.GetPattern(10015) else None

        def panel():
            # The list is "PlayQueueListView" or "PlayQueueListViewAutoplay"
            # depending on the Autoplay setting.
            for candidate in self._find_all(window, ctype=_TYPE.ListControl):
                if (candidate.CurrentAutomationId or "").startswith(QUEUE_LIST_AUTOMATION_ID):
                    return candidate
            return None

        def press():
            toggle.Toggle(waitTime=0) if toggle else button.GetInvokePattern().Invoke(waitTime=0)

        queue_list, opened = panel(), False
        try:
            if queue_list is None:
                if not (toggle and toggle.ToggleState == 1):  # not merely still animating in
                    press()
                    opened = True
                queue_list = self._wait(panel, 4)
                if queue_list is None:
                    raise RuntimeError("queue panel did not open")
            rows = self._settled_rows(queue_list) if opened else self._rows(queue_list)
            items = [{"t": t["t"], "a": t["a"], "d": t["d"]} for t in map(self._track, rows)]
        finally:
            if opened:  # leave the PC's UI the way we found it
                press()
        return {"ok": True, "items": items}


AUDIO, SMTC, UIA = Audio(), Smtc(), Uia()


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


PAGE_BYTES = PAGE.encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"        # keep-alive: no TCP handshake per tap
    disable_nagle_algorithm = True       # small responses must not wait on Nagle
    timeout = 60                         # reap idle keep-alive connections

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain",
              cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, fn, *args) -> None:
        try:
            out = fn(*args)
        except Exception as exc:
            out = {"ok": False, "err": str(exc) or type(exc).__name__}
        self._send(200, _json_bytes(out), "application/json; charset=utf-8")

    def _events(self) -> None:
        """Server-Sent Events: push the state snapshot whenever it changes."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        HUB.listen(+1)
        SMTC.poke(props=True)
        AUDIO.post("poll")
        UIA.post(UIA.refresh_modes)
        version = -1
        try:
            while True:
                # Also resend periodically: keeps positions in sync and lets
                # us notice clients that went away.
                version, state = HUB.wait(version, 10.0)
                self.wfile.write(b"data: " + _json_bytes(state) + b"\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            HUB.listen(-1)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if path == "/":
            self._send(200, PAGE_BYTES, "text/html; charset=utf-8", "no-cache")
        elif path == "/events":
            self._events()
        elif path == "/status":
            self._send(200, _json_bytes(HUB.snapshot()), "application/json; charset=utf-8")
        elif path.startswith("/artwork"):
            art = SMTC.artwork
            if art:  # the URL carries the content hash, so it can be cached forever
                self._send(200, art[1], art[2], "public, max-age=31536000, immutable"
                           if path.endswith(art[0]) else "no-store")
            else:
                self._send(404)
        elif path == "/playlists":
            self._json(UIA.call, UIA.load_playlists)
        elif path == "/modes":
            self._json(UIA.call, UIA.refresh_modes)
        elif path == "/queue":
            self._json(UIA.call, UIA.queue)
        elif path == "/search":
            text = query.get("q", [""])[0].strip()
            self._json(UIA.call, UIA.search, text) if text else self._send(400)
        elif path == "/tracks":
            playlist = query.get("pl", [""])[0]
            if playlist:
                self._json(UIA.call, UIA.load_tracks, playlist, "fresh" in query)
            else:
                self._send(400)
        else:
            self._send(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        path = urlparse(self.path).path
        if path in ("/play", "/splay"):
            try:
                body = json.loads(raw or b"{}")
                if path == "/play":
                    args = (UIA.play_track, str(body["pl"]), str(body["id"]))
                else:
                    args = (UIA.search_play, str(body["q"]), int(body["i"]), str(body["id"]))
            except (ValueError, KeyError, TypeError):
                return self._send(400)
            return self._json(UIA.call, *args)

        parts = path.strip("/").split("/")  # cmd/<name>[/<arg>]
        if len(parts) < 2 or parts[0] != "cmd":
            return self._send(404)
        command, arg = parts[1], parts[2] if len(parts) == 3 else None
        try:
            if command in ("toggle", "next", "prev") and arg is None:
                SMTC.command(command)
            elif command == "vol" and arg and arg.isdigit():
                AUDIO.post("vol", int(arg))
            elif command == "mute" and arg is None:
                AUDIO.post("mute")
            elif command == "seek" and arg and arg.isdigit():
                UIA.call(UIA.seek, int(arg), timeout=10)
                HUB.update(position=(float(arg), time.monotonic()), hold=2.5)
                SMTC.poke()
            elif command in ("shuffle", "repeat") and arg is None:
                UIA.call(UIA.toggle_shuffle if command == "shuffle" else UIA.cycle_repeat,
                         timeout=10)
                # The button's state lags the click a moment; re-read it then.
                threading.Timer(0.4, UIA.post, (UIA.refresh_modes,)).start()
            else:
                return self._send(404)
        except Exception:
            pass  # window, session or control not available right now; ignore
        self._send(204)

    def log_message(self, *args) -> None:
        pass  # keep the console quiet


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    finally:
        sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Web remote for Apple Music on Windows")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"HTTP port to listen on (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    for worker in (AUDIO, SMTC, UIA):
        worker.start()

    print(f"Open on your phone:  http://{lan_ip()}:{args.port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
