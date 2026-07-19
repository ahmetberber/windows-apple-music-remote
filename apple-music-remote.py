#!/usr/bin/env python3
"""Apple Music Remote for Windows.

A tiny self-hosted web remote for the Apple Music app on Windows. Serves a
mobile-friendly web UI on your local network with now-playing info, transport
controls, volume, in-track seeking, shuffle/repeat, and a playlist browser.

The Apple Music app for Windows does not support Apple's iTunes Remote app
(it exposes no DACP service), so this project bridges the gap with three
Windows-native mechanisms:

- SMTC (System Media Transport Controls, via ``winsdk``) for playback state,
  track metadata, artwork, and play/pause/next/previous.
- Core Audio (via ``pycaw``) for reading and setting the system volume.
- UI Automation (via ``uiautomation``) for everything Apple Music's SMTC
  integration ignores: seeking, shuffle, repeat, and playlist browsing.

Usage (must run on Windows with the Apple Music app installed)::

    pip install -r requirements.txt
    python apple-music-remote.py [--port 8765]

Then open the printed URL in your phone's browser (same Wi-Fi network).
See README.md for firewall setup and limitations.
"""

import argparse
import asyncio
import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator, Optional
from urllib.parse import parse_qs, urlparse

import uiautomation
from comtypes import CoInitialize
from pycaw.pycaw import AudioUtilities
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSession as Session,
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

DEFAULT_PORT = 8765
APPLE_MUSIC_WINDOW = "Apple Music"
SCRUBBER_AUTOMATION_ID = "LCDScrubber"
TRACK_DURATION_RE = re.compile(r"^\d+:\d{2}$")

# Repeat state is read from the transport-bar button's accessible name.
# These names come from the English UI; other locales fall back to "none"
# (the cycle button itself still works regardless of locale).
REPEAT_STATES = {"Do Not Repeat": "none", "Repeat All": "all", "Repeat One": "one"}

PAGE = """<!doctype html><html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=apple-mobile-web-app-status-bar-style content=black-translucent>
<meta name=theme-color content="#000">
<title>Apple Music Remote</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent;user-select:none;-webkit-user-select:none}
html,body{height:100%}
body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;color:#fff;background:#000;overflow:hidden}
#bg{position:fixed;inset:-60px;background:#000 center/cover no-repeat;
    filter:blur(70px) saturate(1.7) brightness(.45);transform:scale(1.2)}
header{position:fixed;top:0;left:0;right:0;z-index:10;display:flex;align-items:center;
       padding:calc(env(safe-area-inset-top,0px) + 8px) 10px 8px}
header button{background:none;border:none;padding:10px;cursor:pointer}
header svg{width:24px;height:24px;fill:#fff;display:block}
#htitle{flex:1;text-align:center;font-weight:700;font-size:1.02rem;white-space:nowrap;
        overflow:hidden;text-overflow:ellipsis}
.hspace{width:44px;flex-shrink:0}
.view{position:fixed;inset:0;display:flex;flex-direction:column;
      transition:opacity .22s,transform .22s}
.view.off{opacity:0;pointer-events:none;transform:scale(.98)}
#view-now{align-items:center;justify-content:center}
main{width:min(88vw,380px);display:flex;flex-direction:column;gap:24px}
#art{width:100%;aspect-ratio:1;border-radius:16px;object-fit:cover;background:rgba(255,255,255,.06);
     box-shadow:0 24px 60px rgba(0,0,0,.6);transition:transform .4s cubic-bezier(.3,1.3,.5,1)}
body.paused #art{transform:scale(.9)}
#t{font-size:1.3rem;font-weight:700;text-align:center}
#a{color:rgba(255,255,255,.6);text-align:center;margin-top:4px;font-size:.95rem;
   overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#prog{visibility:hidden}
#times{display:flex;justify-content:space-between;font-size:.72rem;
       color:rgba(255,255,255,.5);margin-top:7px;font-variant-numeric:tabular-nums}
#ctrl{display:flex;align-items:center;justify-content:center;gap:24px}
button{background:none;border:none;color:#fff;padding:6px;cursor:pointer;transition:transform .1s,opacity .1s}
button:active{transform:scale(.82);opacity:.6}
button svg{width:32px;height:32px;fill:#fff;display:block}
#pp svg{width:46px;height:46px}
#shf svg,#rpt svg{width:21px;height:21px;fill:rgba(255,255,255,.6)}
#shf.on svg,#rpt.on svg{fill:#ff7ab8}
#vol{display:flex;align-items:center;gap:14px}
#vol svg{width:20px;height:20px;fill:rgba(255,255,255,.75);display:block}
body.muted #vol input{opacity:.35}
input[type=range]{flex:1;-webkit-appearance:none;appearance:none;height:6px;border-radius:3px;
                  background:rgba(255,255,255,.25);outline:none;transition:opacity .2s}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:24px;height:24px;
                  border-radius:50%;background:#fff;box-shadow:0 2px 10px rgba(0,0,0,.45)}
#pr{width:100%;height:5px}
#pr::-webkit-slider-thumb{width:17px;height:17px}
#sq{width:100%;padding:12px 16px;border-radius:12px;border:none;outline:none;font-size:1rem;
    background:rgba(255,255,255,.12);color:#fff;margin-bottom:12px;-webkit-appearance:none;
    font-family:inherit}
#sq::placeholder{color:rgba(255,255,255,.4)}
#view-pls,#view-trs,#view-src,#view-que{padding-top:calc(env(safe-area-inset-top,0px) + 56px)}
.listwrap{flex:1;overflow-y:auto;-webkit-overflow-scrolling:touch;width:100%;max-width:540px;
          margin:0 auto;padding:6px 16px calc(env(safe-area-inset-bottom,0px) + 30px)}
.row{display:flex;align-items:center;gap:14px;padding:12px 14px;border-radius:14px;
     background:rgba(255,255,255,.09);backdrop-filter:blur(30px);-webkit-backdrop-filter:blur(30px);
     margin-bottom:8px;cursor:pointer}
.row:active{background:rgba(255,255,255,.18)}
.row.busy{opacity:.45}
.badge{width:44px;height:44px;border-radius:11px;display:flex;align-items:center;justify-content:center;
       font-weight:700;font-size:1.15rem;background:linear-gradient(135deg,#fc5c7d,#6a82fb);flex-shrink:0}
.rt{flex:1;min-width:0}
.rt b{display:block;font-size:.94rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rt span{display:block;font-size:.8rem;color:rgba(255,255,255,.55);white-space:nowrap;
         overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.row.playing b{color:#ff7ab8}
.dur{font-size:.78rem;color:rgba(255,255,255,.5);font-variant-numeric:tabular-nums;flex-shrink:0}
.chev{width:14px;height:14px;fill:rgba(255,255,255,.4);flex-shrink:0}
.spin{width:28px;height:28px;border:3px solid rgba(255,255,255,.2);border-top-color:#fff;
      border-radius:50%;animation:r .8s linear infinite;margin:44px auto 14px}
@keyframes r{to{transform:rotate(360deg)}}
.hint{text-align:center;color:rgba(255,255,255,.5);font-size:.85rem;padding:0 30px}
</style>
<div id=bg></div>
<header>
<button id=hbtn></button>
<div id=htitle></div>
<span id=hright><button id=qbtn onclick="openQue()"></button><button id=sbtn onclick="openSrc()"></button></span>
</header>

<div class=view id=view-now>
<main>
<img id=art>
<div><div id=t>&mdash;</div><div id=a></div></div>
<div id=prog>
<input id=pr type=range min=0 max=100 value=0 step=1 oninput="scrub(this.value)" onchange="seekTo(this.value)">
<div id=times><span id=cur>0:00</span><span id=tot>0:00</span></div></div>
<div id=ctrl>
<button id=shf onclick="mode('shuffle')"></button>
<button onclick="c('prev')"><svg viewBox="0 0 24 24"><path d="M20 5v14l-9.5-7L20 5zM10.5 5v14L1 12l9.5-7z"/></svg></button>
<button id=pp onclick="c('toggle')"><svg viewBox="0 0 24 24"><path d="M7 4v16l14-8z"/></svg></button>
<button onclick="c('next')"><svg viewBox="0 0 24 24"><path d="M4 5v14l9.5-7L4 5zM13.5 5v14L23 12l-9.5-7z"/></svg></button>
<button id=rpt onclick="mode('repeat')"></button>
</div>
<div id=vol>
<button id=mute onclick="c('mute')"><svg viewBox="0 0 24 24"><path d="M3 9v6h4l6 5V4L7 9H3z"/></svg></button>
<input id=vr type=range min=0 max=100 value=50 oninput="vset(this.value)">
<svg viewBox="0 0 24 24"><path d="M3 9v6h4l6 5V4L7 9H3z"/><path d="M16 8a5.5 5.5 0 010 8l-1.3-1.5a3.5 3.5 0 000-5L16 8z"/><path d="M18.5 5.5a9 9 0 010 13l-1.3-1.5a7 7 0 000-10l1.3-1.5z"/></svg>
</div>
</main>
</div>

<div class="view off" id=view-pls><div class=listwrap id=pls-list></div></div>
<div class="view off" id=view-trs><div class=listwrap id=trs-list></div></div>
<div class="view off" id=view-src><div class=listwrap>
<input id=sq type=search enterkeyhint=search placeholder="Search Apple Music"
       onkeydown="if(event.key==='Enter')doSearch()">
<div id=src-list></div>
</div></div>
<div class="view off" id=view-que><div class=listwrap id=que-list></div></div>

<script>
const $=i=>document.getElementById(i);
const PLAY='<svg viewBox="0 0 24 24"><path d="M7 4v16l14-8z"/></svg>';
const PAUSE='<svg viewBox="0 0 24 24"><path d="M6 4h4.5v16H6zM13.5 4H18v16h-4.5z"/></svg>';
const LISTICON='<svg viewBox="0 0 24 24"><path d="M3 6h12v2H3zM3 11h12v2H3zM3 16h8v2H3zM19 6v8.3a3.2 3.2 0 101.5 2.7V8h2V6h-3.5z"/></svg>';
const BACKICON='<svg viewBox="0 0 24 24"><path d="M15 4l-8 8 8 8 1.6-1.6L10.2 12l6.4-6.4z"/></svg>';
const CHEV='<svg class=chev viewBox="0 0 24 24"><path d="M9 4l8 8-8 8-1.6-1.6L13.8 12 7.4 5.6z"/></svg>';
const SHUF='<svg viewBox="0 0 24 24"><path d="M10.59 9.17 5.41 4 4 5.41l5.17 5.17 1.42-1.41zM14.5 4l2.04 2.04L4 18.59 5.41 20 17.96 7.46 20 9.5V4h-5.5zm.33 9.41-1.42 1.42 3.14 3.14L14.5 20H20v-5.5l-2.04 2.05-3.13-3.14z"/></svg>';
const REP='<svg viewBox="0 0 24 24"><path d="M17 17H7v-3l-4 4 4 4v-3h12v-6h-2v4zM7 7h10v3l4-4-4-4v3H5v6h2V7z"/></svg>';
const REP1='<svg viewBox="0 0 24 24"><path d="M13 15V9h-1l-2 1v1h1.5v4H13zM17 17H7v-3l-4 4 4 4v-3h12v-6h-2v4zM7 7h10v3l4-4-4-4v3H5v6h2V7z"/></svg>';
const SEARCHICON='<svg viewBox="0 0 24 24"><path d="M10 2a8 8 0 105.29 14.03l4.34 4.34 1.41-1.41-4.34-4.34A8 8 0 0010 2zm0 2a6 6 0 110 12 6 6 0 010-12z"/></svg>';
const QUEUEICON='<svg viewBox="0 0 24 24"><path d="M3 5h12v2H3zM3 10h12v2H3zM3 15h7v2H3zM16.5 12.5l6 3.5-6 3.5z"/></svg>';
function esc(s){return(s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
let last='',dragTs=0,pos=0,dur=0,posTs=0,playing=false,scrubbing=false,seekTs=0,nowTitle='';
let view='now',curPl=-1,plsData=null,srcItems=[],lastQ='';const trCache={};

function show(v){view=v;['now','pls','trs','src','que'].forEach(x=>$('view-'+x).classList.toggle('off',x!==v));
  $('hbtn').innerHTML=v==='now'?LISTICON:BACKICON;
  $('hright').style.visibility=v==='now'?'visible':'hidden';
  $('htitle').textContent=v==='pls'?'Playlists':v==='trs'?(plsData[curPl]||{}).name||''
    :v==='src'?'Search':v==='que'?'Up Next':''}
$('hbtn').onclick=()=>{if(view==='now')openPls();else if(view==='trs')openPls();else show('now')};

function openSrc(){show('src');setTimeout(()=>$('sq').focus(),250)}
async function doSearch(){const q=$('sq').value.trim();if(!q)return;lastQ=q;$('sq').blur();
  const el=$('src-list');el.innerHTML='<div class=spin></div>';
  try{const r=await(await fetch('/search?q='+encodeURIComponent(q))).json();if(!r.ok)throw 0;srcItems=r.items;
    el.innerHTML=srcItems.map((t,j)=>'<div class=row onclick="splay('+j+',this)">'
      +'<div class=rt><b>'+esc(t.t)+'</b><span>'+esc(t.a)+'</span></div></div>').join('')
      ||'<div class=hint>No results</div>'}
  catch(e){el.innerHTML='<div class=hint>Search failed</div>'}}
async function splay(j,el){el.classList.add('busy');
  try{await fetch('/splay',{method:'POST',body:JSON.stringify({q:lastQ,i:j,id:srcItems[j].id})})}catch(e){}
  el.classList.remove('busy');show('now');setTimeout(poll,1200)}

async function openQue(){show('que');const el=$('que-list');el.innerHTML='<div class=spin></div>';
  try{const r=await(await fetch('/queue')).json();if(!r.ok)throw 0;
    el.innerHTML=r.items.map(t=>'<div class=row style="cursor:default">'
      +'<div class=rt><b>'+esc(t.t)+'</b><span>'+esc(t.a)+'</span></div>'
      +'<div class=dur>'+esc(t.d)+'</div></div>').join('')
      ||'<div class=hint>Queue is empty</div>'}
  catch(e){el.innerHTML='<div class=hint>Could not read the queue</div>'}}

async function openPls(){show('pls');const el=$('pls-list');
  if(!plsData){el.innerHTML='<div class=spin></div>';
    try{const r=await(await fetch('/playlists')).json();if(!r.ok)throw 0;plsData=r.items}
    catch(e){el.innerHTML='<div class=hint>Could not load playlists. Is Apple Music running?</div>';return}
    show('pls')}
  el.innerHTML=plsData.map((p,i)=>'<div class=row onclick="openTrs('+i+')">'
    +'<div class=badge style="filter:hue-rotate('+(i*47)+'deg)">'+esc((p.name||'♪')[0])+'</div>'
    +'<div class=rt><b>'+esc(p.name)+'</b></div>'+CHEV+'</div>').join('')
    ||'<div class=hint>No playlists found in the sidebar</div>'}

async function openTrs(i){curPl=i;show('trs');const el=$('trs-list');
  if(!trCache[i]){el.innerHTML='<div class=spin></div><div class=hint>Loading tracks… the first read of a playlist can take a while</div>';
    try{const r=await(await fetch('/tracks/'+i)).json();if(!r.ok)throw 0;trCache[i]=r.items}
    catch(e){el.innerHTML='<div class=hint>Could not load tracks</div>';return}
    if(view!=='trs'||curPl!==i)return}
  renderTrs(i)}

function renderTrs(i){$('trs-list').innerHTML=trCache[i].map((t,j)=>
  '<div class="row'+(t.t===nowTitle?' playing':'')+'" onclick="playTrack('+i+','+j+',this)">'
  +'<div class=rt><b>'+esc(t.t)+'</b><span>'+esc(t.a)+'</span></div>'
  +'<div class=dur>'+esc(t.d)+'</div></div>').join('')
  ||'<div class=hint>No tracks in this playlist</div>'}

async function playTrack(i,j,el){el.classList.add('busy');
  try{await fetch('/play',{method:'POST',body:JSON.stringify({pl:i,id:trCache[i][j].id})})}catch(e){}
  el.classList.remove('busy');show('now');setTimeout(poll,1200)}

async function c(x){await fetch('/cmd/'+x,{method:'POST'});setTimeout(poll,350)}
let modes={shuffle:false,repeat:'none'};
function renderModes(){$('shf').innerHTML=SHUF;$('shf').classList.toggle('on',!!modes.shuffle);
  $('rpt').innerHTML=modes.repeat==='one'?REP1:REP;$('rpt').classList.toggle('on',modes.repeat!=='none')}
async function loadModes(){try{const m=await(await fetch('/modes')).json();if(m.ok)modes=m}catch(e){}renderModes()}
async function mode(x){
  // optimistic update; the button state in the app lags a moment behind
  if(x==='shuffle')modes.shuffle=!modes.shuffle;
  else modes.repeat=modes.repeat==='none'?'all':modes.repeat==='all'?'one':'none';
  renderModes();
  await fetch('/cmd/'+x,{method:'POST'});setTimeout(loadModes,700)}
let vt;
function vset(v){dragTs=Date.now();clearTimeout(vt);vt=setTimeout(()=>fetch('/cmd/vol/'+v,{method:'POST'}),120)}
function fmt(x){x=Math.max(0,x)|0;return(x/60|0)+':'+String(x%60).padStart(2,'0')}
function fill(el,v,max){el.style.background='linear-gradient(90deg,rgba(255,255,255,.85) '+(max>0?v/max*100:0)+'%,rgba(255,255,255,.25) 0)'}
function scrub(v){scrubbing=true;$('cur').textContent=fmt(+v);fill($('pr'),+v,dur)}
async function seekTo(v){scrubbing=false;pos=+v;posTs=Date.now();seekTs=Date.now();
  await fetch('/cmd/seek/'+(v|0),{method:'POST'});setTimeout(poll,600)}
async function poll(){try{
  const s=await(await fetch('/status')).json();
  if(!s.ok){$('t').textContent='Apple Music is not running';$('a').textContent='';return}
  nowTitle=s.title||'';
  $('t').textContent=s.title||'\\u2014';$('a').textContent=s.artist||'';
  playing=s.playing;document.body.classList.toggle('paused',!playing);
  document.body.classList.toggle('muted',!!s.muted);
  $('pp').innerHTML=playing?PAUSE:PLAY;
  dur=s.dur;
  if(!scrubbing&&Date.now()-seekTs>3000){pos=s.pos;posTs=Date.now()}
  if(s.vol!=null&&Date.now()-dragTs>1500){$('vr').value=s.vol;fill($('vr'),s.vol,100)}
  const k=s.title+'|'+s.artist;
  if(k!==last){last=k;const u='/artwork?'+Date.now();$('art').src=u;$('bg').style.backgroundImage='url('+u+')'}
}catch(e){}}
function tick(){
  if(dur>0){$('pr').max=dur|0;$('tot').textContent=fmt(dur);$('prog').style.visibility='visible';
    if(!scrubbing){let p=Math.min(pos+(playing?(Date.now()-posTs)/1000:0),dur);
      $('pr').value=p|0;$('cur').textContent=fmt(p);fill($('pr'),p,dur)}}
  else $('prog').style.visibility='hidden'}
$('sbtn').innerHTML=SEARCHICON;$('qbtn').innerHTML=QUEUEICON;
show('now');renderModes();setInterval(poll,2000);setInterval(tick,500);poll();loadModes();
</script>"""


# --------------------------------------------------------------------------
# System volume (pycaw / Core Audio)
# --------------------------------------------------------------------------

def _speakers():
    # ThreadingHTTPServer handles each request on a fresh thread, so COM must
    # be initialized here rather than once at startup.
    CoInitialize()
    return AudioUtilities.GetSpeakers()


def _volume_endpoint():
    return _speakers().EndpointVolume


def get_volume() -> tuple[int, bool]:
    """Return (volume percent 0-100, muted)."""
    dev = _speakers()
    # pycaw exposes volume_percent (PR #109) on newer releases; fall back to the
    # scalar API on older ones so autostart never crashes on an outdated pycaw.
    percent = (dev.volume_percent if hasattr(dev, "volume_percent")
               else dev.EndpointVolume.GetMasterVolumeLevelScalar() * 100)
    return round(percent), bool(dev.EndpointVolume.GetMute())


def set_volume(percent: int) -> None:
    percent = max(0, min(100, percent))
    dev = _speakers()
    if hasattr(dev, "volume_percent"):
        dev.volume_percent = percent
    else:
        dev.EndpointVolume.SetMasterVolumeLevelScalar(percent / 100, None)


def toggle_mute() -> None:
    endpoint = _volume_endpoint()
    endpoint.SetMute(not endpoint.GetMute(), None)


# --------------------------------------------------------------------------
# Playback state and transport (SMTC / winsdk)
# --------------------------------------------------------------------------

async def _session() -> Optional[Session]:
    manager = await SessionManager.request_async()
    for session in manager.get_sessions():
        if "applemusic" in (session.source_app_user_model_id or "").lower():
            return session
    # Fall back to whatever is currently playing so the transport controls
    # still work if Apple Music has no active session.
    return manager.get_current_session()


async def _status() -> dict:
    session = await _session()
    if not session:
        return {"ok": False}
    props = await session.try_get_media_properties_async()
    playing = session.get_playback_info().playback_status == PlaybackStatus.PLAYING
    timeline = session.get_timeline_properties()
    try:
        vol, muted = get_volume()
    except Exception:
        vol, muted = None, False
    return {"ok": True, "title": props.title, "artist": props.artist,
            "playing": playing,
            "pos": timeline.position.total_seconds(),
            "dur": timeline.end_time.total_seconds(),
            "vol": vol, "muted": muted}


async def _artwork() -> Optional[bytes]:
    session = await _session()
    if not session:
        return None
    props = await session.try_get_media_properties_async()
    if not props.thumbnail:
        return None
    stream = await props.thumbnail.open_read_async()
    buf = Buffer(stream.size)
    await stream.read_async(buf, stream.size, InputStreamOptions.READ_AHEAD)
    data = bytearray(buf.length)
    DataReader.from_buffer(buf).read_bytes(data)
    return bytes(data)


async def _transport(command: str) -> None:
    session = await _session()
    if not session:
        return
    await {"toggle": session.try_toggle_play_pause_async,
           "next": session.try_skip_next_async,
           "prev": session.try_skip_previous_async}[command]()


# --------------------------------------------------------------------------
# UI Automation bridge (seek, shuffle, repeat, playlists)
#
# Apple Music's SMTC integration rejects position changes and does not expose
# shuffle/repeat or any library access, so these features drive the app's own
# window through UI Automation patterns (no mouse/keyboard simulation).
# UIA operations must not interleave, hence the global lock.
# --------------------------------------------------------------------------

UIA_LOCK = threading.Lock()
PLAYLISTS: list[dict] = []   # [{"aid": <AutomationId>, "name": <str>}]
TRACK_CACHE: dict[int, list[dict]] = {}  # playlist index -> parsed tracks


def _window() -> uiautomation.WindowControl:
    window = uiautomation.WindowControl(searchDepth=1, SubName=APPLE_MUSIC_WINDOW)
    if not window.Exists(3):
        raise RuntimeError("Apple Music window not found")
    return window


def _load_playlists(window) -> None:
    """Read user playlists from the sidebar. Requires the sidebar's
    'Playlists' section to be expanded (it is by default)."""
    PLAYLISTS.clear()
    for control, _ in uiautomation.WalkControl(window, maxDepth=12):
        if (control.ControlTypeName == "ListItemControl"
                and "IKIND:ePlaylist" in (control.AutomationId or "")):
            PLAYLISTS.append({"aid": control.AutomationId, "name": control.Name})


def _open_playlist(window, index: int):
    """Select a playlist in the sidebar and return its track ListControl."""
    if not PLAYLISTS:
        _load_playlists(window)
    playlist = PLAYLISTS[index]
    item = uiautomation.ListItemControl(searchFromControl=window,
                                        AutomationId=playlist["aid"])
    item.GetSelectionItemPattern().Select()
    # The track list is loaded once its header row ("<name> N items ...")
    # matches the selected playlist.
    for _ in range(20):
        time.sleep(0.4)
        for control, _ in uiautomation.WalkControl(window, maxDepth=16):
            if control.ControlTypeName == "ListControl":
                kids = control.GetChildren()
                if kids and (kids[0].Name or "").startswith(playlist["name"]):
                    return control
    raise RuntimeError("track list did not load")


def _parse_row(row) -> Optional[dict]:
    """Extract title/artist/duration from a track row's text elements."""
    kids = row.GetChildren()
    grid = kids[0] if kids else None
    texts = [t.Name for t in (grid.GetChildren() if grid else [])
             if t.ControlTypeName == "TextControl" and t.Name]
    if not texts:
        return None
    duration = texts[-1] if TRACK_DURATION_RE.match(texts[-1]) else ""
    return {"t": texts[0], "a": texts[1] if len(texts) > 2 else "",
            "d": duration, "id": row.Name}


def _walk_rows(track_list, step: int = 25) -> Iterator[list]:
    """Yield visible rows while scrolling through a virtualized list.

    Only rendered rows exist in the UIA tree, so the list is scrolled in
    ``step``-percent increments and re-read at each stop. Large playlists
    (thousands of tracks) will take proportionally longer on first read.
    """
    try:
        scroll = track_list.GetScrollPattern()
        if not scroll.VerticallyScrollable:
            scroll = None
    except Exception:
        scroll = None
    if scroll is not None:
        scroll.SetScrollPercent(-1, 0)
        time.sleep(0.2)
    percent = 0
    while True:
        yield track_list.GetChildren()
        if scroll is None or percent >= 100:
            return
        percent = min(percent + step, 100)
        scroll.SetScrollPercent(-1, percent)
        time.sleep(0.3)


def _collect_tracks(track_list) -> list[dict]:
    seen: set[str] = set()
    items: list[dict] = []
    for rows in _walk_rows(track_list):
        for row in rows:
            name = row.Name or ""
            if name.startswith("Track ") and name not in seen:
                seen.add(name)
                parsed = _parse_row(row)
                if parsed:
                    items.append(parsed)
    return items


def _invoke_row_play(window, row) -> None:
    """Play a song row: open its "More" (…) menu and invoke 'Play <title>'."""
    try:
        row.GetScrollItemPattern().ScrollIntoView()
        time.sleep(0.3)
    except Exception:
        pass
    grid = row.GetChildren()[0]
    buttons = [b for b in grid.GetChildren() if b.ControlTypeName == "ButtonControl"]
    if not buttons:
        raise RuntimeError("row has no menu button")
    buttons[-1].GetInvokePattern().Invoke()
    time.sleep(0.8)
    texts = [t.Name for t in grid.GetChildren()
             if t.ControlTypeName == "TextControl" and t.Name]
    title = texts[0] if texts else ""
    for item, _ in uiautomation.WalkControl(window, maxDepth=18):
        if item.ControlTypeName == "MenuItemControl":
            name = item.Name or ""
            # Match 'Play "<title>"' by title so it works in any locale;
            # the quoted form is the English fallback for very short titles.
            if (title and title[:10] in name) or name.startswith("Play “"):
                item.GetInvokePattern().Invoke()
                return
    raise RuntimeError("Play menu item not found")


def _play_track(window, track_list, row_id: str) -> None:
    row = None
    for rows in _walk_rows(track_list):
        row = next((r for r in rows if r.Name == row_id), None)
        if row:
            break
    if not row:
        raise RuntimeError("track not found in playlist")
    _invoke_row_play(window, row)


def _search_section(window, name: str):
    """Find a named section ('Songs', 'Top Results', ...) on the results page."""
    for control, _ in uiautomation.WalkControl(window, maxDepth=8):
        if control.ControlTypeName == "GroupControl" and control.Name == "Content":
            for section, _ in uiautomation.WalkControl(control, maxDepth=3):
                if (section.ControlTypeName == "ListItemControl"
                        and section.Name == name):
                    return section
            return None
    return None


def _do_search(window, query: str):
    """Type into the sidebar search box, run the search, return the Songs
    section (or None when the results contain no songs)."""
    box = uiautomation.EditControl(searchFromControl=window, AutomationId="TextBox")
    if not box.Exists(2):
        raise RuntimeError("search box not found")
    box.GetValuePattern().SetValue(query)
    uiautomation.ButtonControl(searchFromControl=window,
                               AutomationId="QueryButton").GetInvokePattern().Invoke()
    time.sleep(1.0)  # let a previous results page clear before polling
    for _ in range(20):
        time.sleep(0.4)
        songs = _search_section(window, "Songs")
        if songs:
            return songs
        if _search_section(window, "Top Results"):
            time.sleep(0.6)  # sections render one by one; give Songs one more beat
            return _search_section(window, "Songs")
    raise RuntimeError("search results did not load")


def _search_rows(section) -> list:
    rows = []
    for control, _ in uiautomation.WalkControl(section, maxDepth=6):
        if (control.ControlTypeName == "ListItemControl"
                and control.Name and control.Name != section.Name):
            rows.append(control)
    return rows


def _transport_button(window, automation_id: str):
    button = uiautomation.ButtonControl(searchFromControl=window,
                                        AutomationId=automation_id)
    if not button.Exists(2):
        raise RuntimeError(automation_id + " not found")
    return button


def seek(seconds: int) -> None:
    """Seek by setting the transport bar's scrubber slider directly."""
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        slider = uiautomation.SliderControl(searchFromControl=window,
                                            AutomationId=SCRUBBER_AUTOMATION_ID)
        slider.GetRangeValuePattern().SetValue(seconds)


def toggle_shuffle() -> None:
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        _transport_button(_window(), "ShuffleButton").GetTogglePattern().Toggle()


def cycle_repeat() -> None:
    """Advance repeat mode: off -> repeat all -> repeat one -> off."""
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        button = _transport_button(_window(), "RepeatButton")
        button.GetLegacyIAccessiblePattern().DoDefaultAction()


def api_modes() -> dict:
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        shuffle = (_transport_button(window, "ShuffleButton")
                   .GetTogglePattern().ToggleState == 1)
        repeat = REPEAT_STATES.get(_transport_button(window, "RepeatButton").Name,
                                   "none")
    return {"ok": True, "shuffle": shuffle, "repeat": repeat}


def api_playlists() -> dict:
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        _load_playlists(_window())
    return {"ok": True, "items": [{"name": p["name"]} for p in PLAYLISTS]}


def api_tracks(index: int, fresh: bool = False) -> dict:
    if not fresh and index in TRACK_CACHE:
        return {"ok": True, "items": TRACK_CACHE[index]}
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        track_list = _open_playlist(window, index)
        TRACK_CACHE[index] = _collect_tracks(track_list)
    return {"ok": True, "items": TRACK_CACHE[index]}


def api_play(index: int, row_id: str) -> dict:
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        track_list = _open_playlist(window, index)
        _play_track(window, track_list, row_id)
    return {"ok": True}


def api_search(query: str) -> dict:
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        section = _do_search(window, query)
        items = []
        for row in (_search_rows(section) if section else []):
            kids = row.GetChildren()
            grid = kids[0] if kids else None
            texts = [t.Name for t in (grid.GetChildren() if grid else [])
                     if t.ControlTypeName == "TextControl" and t.Name]
            if texts:
                items.append({"t": texts[0],
                              "a": texts[1] if len(texts) > 1 else "",
                              "id": row.Name})
    return {"ok": True, "items": items}


def api_search_play(query: str, index: int, row_id: str) -> dict:
    """Re-run the search (results are deterministic) and play the chosen row.

    Row names are not unique across results, so the row is matched by
    position first and by name as a fallback.
    """
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        section = _do_search(window, query)
        rows = _search_rows(section) if section else []
        row = None
        if 0 <= index < len(rows) and rows[index].Name == row_id:
            row = rows[index]
        else:
            row = next((r for r in rows if r.Name == row_id), None)
        if not row:
            raise RuntimeError("search result not found")
        _invoke_row_play(window, row)
    return {"ok": True}


def api_queue() -> dict:
    """Read the Up Next panel. Opens it briefly if it isn't already open."""
    with UIA_LOCK, uiautomation.UIAutomationInitializerInThread():
        window = _window()
        queue_list = uiautomation.ListControl(searchFromControl=window,
                                              AutomationId="PlayQueueListViewAutoplay")
        opened = False
        if not queue_list.Exists(1):
            button = _transport_button(window, "PlayQueueToggleButton")
            (button.GetTogglePattern().Toggle() if button.GetPattern(10015)
             else button.GetInvokePattern().Invoke())
            opened = True
            time.sleep(1.2)
            queue_list = uiautomation.ListControl(
                searchFromControl=window, AutomationId="PlayQueueListViewAutoplay")
            if not queue_list.Exists(3):
                raise RuntimeError("queue panel did not open")
        try:
            items = []
            for row in queue_list.GetChildren():
                grid = row.GetChildren()
                grid = grid[0] if grid else None
                texts = [t.Name for t in (grid.GetChildren() if grid else [])
                         if t.ControlTypeName == "TextControl" and t.Name]
                if texts:
                    duration = texts[-1] if TRACK_DURATION_RE.match(texts[-1]) else ""
                    items.append({"t": texts[0],
                                  "a": texts[1] if len(texts) > 2 else "",
                                  "d": duration})
        finally:
            if opened:  # leave the PC's UI the way we found it
                button = _transport_button(window, "PlayQueueToggleButton")
                (button.GetTogglePattern().Toggle() if button.GetPattern(10015)
                 else button.GetInvokePattern().Invoke())
    return {"ok": True, "items": items}


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, fn) -> None:
        try:
            out = fn()
        except Exception as exc:
            out = {"ok": False, "err": str(exc)}
        self._send(200, json.dumps(out).encode(), "application/json")

    def do_GET(self) -> None:
        if self.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path.startswith("/status"):
            self._json(lambda: asyncio.run(_status()))
        elif self.path.startswith("/playlists"):
            self._json(api_playlists)
        elif self.path.startswith("/modes"):
            self._json(api_modes)
        elif self.path.startswith("/search?"):
            query = parse_qs(urlparse(self.path).query).get("q", [""])[0].strip()
            if query:
                self._json(lambda: api_search(query))
            else:
                self._send(400)
        elif self.path.startswith("/queue"):
            self._json(api_queue)
        elif self.path.startswith("/tracks/"):
            index = self.path.rsplit("/", 1)[-1].split("?")[0]
            fresh = "fresh" in self.path
            if index.isdigit():
                self._json(lambda: api_tracks(int(index), fresh))
            else:
                self._send(404)
        elif self.path.startswith("/artwork"):
            try:
                data = asyncio.run(_artwork())
            except OSError:  # thumbnail stream can vanish during track changes
                data = None
            self._send(200, data, "image/jpeg") if data else self._send(404)
        else:
            self._send(404)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self) -> None:
        if self.path == "/play":
            try:
                body = self._read_body()
                index, row_id = int(body["pl"]), body["id"]
            except (ValueError, KeyError):
                return self._send(400)
            return self._json(lambda: api_play(index, row_id))
        if self.path == "/splay":
            try:
                body = self._read_body()
                query, index, row_id = body["q"], int(body["i"]), body["id"]
            except (ValueError, KeyError):
                return self._send(400)
            return self._json(lambda: api_search_play(query, index, row_id))

        parts = self.path.strip("/").split("/")  # cmd/<name>[/<arg>]
        if len(parts) < 2 or parts[0] != "cmd":
            return self._send(404)
        command, arg = parts[1], parts[2] if len(parts) == 3 else None
        if command in ("toggle", "next", "prev") and arg is None:
            asyncio.run(_transport(command))
        elif command == "vol" and arg and arg.isdigit():
            set_volume(int(arg))
        elif command == "mute" and arg is None:
            toggle_mute()
        elif command == "seek" and arg and arg.isdigit():
            try:
                seek(int(arg))
            except Exception:
                pass  # window or scrubber not available; ignore
        elif command in ("shuffle", "repeat") and arg is None:
            try:
                toggle_shuffle() if command == "shuffle" else cycle_repeat()
            except Exception:
                pass
        else:
            return self._send(404)
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

    # Keep requests snappy when the Apple Music window is missing
    # (the default UIA search timeout is 10 seconds).
    uiautomation.SetGlobalSearchTimeout(2)

    print(f"Open on your phone:  http://{lan_ip()}:{args.port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
