#!/usr/bin/env python3
"""S-100 Host Link v5 dual-pane development UI."""
from __future__ import annotations
import os, threading
from pathlib import Path
from typing import Optional
import gi
gi.require_version('Gtk','4.0'); gi.require_version('Gdk','4.0'); gi.require_version('Adw','1')
from gi.repository import Adw,Gdk,Gio,GLib,GObject,Gtk
from s100_hostlink_gtk4 import (ACK,BLOCK_SIZE,CAN,EOT,NAK,SOH,APP_ID,DirectoryFile,
 HostLinkV2,TransferStats,XModemError,cpm_filename,crc16_xmodem,list_ports,load_settings,save_settings,serial)

def _get(self,name,out,drive,user,size=0):
 st=TransferStats(0,mode='HOST2/GET'); self._begin(); self._send_packet_with_retry(self._packet(0,self._command_payload(3,drive,user,remote_name=name,file_size=size)),0,st)
 dst=Path(out); part=dst.with_name(dst.name+'.part'); part.unlink(missing_ok=True); seq=1; got=0
 try:
  with part.open('wb') as f:
   while True:
    self._check_cancel(); ch=self._read_one(self.response_timeout)
    if ch is None: self._cancel_remote(); raise XModemError('Timed out receiving CP/M file')
    if ch==EOT: self.ser.write(bytes([ACK])); self.ser.flush(); st.bytes_in_file=got; part.replace(dst); return st
    if ch==CAN:
     if self._read_one(1)==CAN: raise XModemError('HOST.COM could not send file')
     continue
    if ch!=SOH: continue
    s,c=self._read_exact(2,self.response_timeout); data=self._read_exact(BLOCK_SIZE,self.response_timeout); crc=self._read_exact(2,self.response_timeout)
    if ((s+c)&255)!=255 or crc16_xmodem(data)!=((crc[0]<<8)|crc[1]): self.ser.write(bytes([NAK])); self.ser.flush(); st.retries+=1; continue
    if s==seq: f.write(data); got+=128; st.blocks_sent+=1; seq=(seq+1)&255; self.ser.write(bytes([ACK])); self.ser.flush(); self.on_progress(got,size or got,st)
    elif s==((seq-1)&255): self.ser.write(bytes([ACK])); self.ser.flush(); st.retries+=1
    else: self.ser.write(bytes([NAK])); self.ser.flush(); st.retries+=1
 except Exception: part.unlink(missing_ok=True); raise
HostLinkV2.CMD_GET=3; HostLinkV2.receive_file=_get

class Win(Adw.ApplicationWindow):
 BAUD=['9600','19200','38400','57600','115200','230400','460800','921600']; LD='HL:L:'; CD='HL:C:'
 def __init__(self,app):
  super().__init__(application=app); self.set_title('S-100 Host Link'); self.set_default_size(1200,800); self.set_size_request(950,640)
  self.cfg=load_settings(); self.ports=[]; self.ldir=Path(self.cfg.get('last_directory',Path.home())).expanduser(); self.ldir=self.ldir if self.ldir.is_dir() else Path.home()
  self.lrows={}; self.crows={}; self.cfiles=[]; self.lsel=None; self.csel=None; self.busy=False; self.worker=None; self.cancel_ev=threading.Event(); self.ui(); self.refresh_ports(); self.restore(); self.lrefresh(); self.crender([])
 def ui(self):
  self.ov=Adw.ToastOverlay(); self.set_content(self.ov); tv=Adw.ToolbarView(); self.ov.set_child(tv); h=Adw.HeaderBar(); h.set_title_widget(Adw.WindowTitle(title='S-100 Host Link',subtitle='Linux ↔ CP/M')); tv.add_top_bar(h)
  root=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=9); [f(10) for f in (root.set_margin_top,root.set_margin_bottom,root.set_margin_start,root.set_margin_end)]; tv.set_content(root)
  g=Adw.PreferencesGroup(title='Connection'); root.append(g); self.pm=Gtk.StringList.new([]); r=Adw.ActionRow(title='USB device'); self.pdd=Gtk.DropDown(model=self.pm); r.add_suffix(self.pdd); g.add(r); self.pdd.connect('notify::selected',self.save)
  self.bm=Gtk.StringList.new(self.BAUD); self.br=Adw.ComboRow(title='Baud rate',model=self.bm); self.br.connect('notify::selected',self.save); g.add(self.br)
  ps=Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,spacing=10); ps.set_vexpand(True); root.append(ps); lf,lb=self.pane('Linux'); rf,rb=self.pane('CP/M'); ps.append(lf); ps.append(rf)
  t=Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,spacing=5); self.path=Gtk.Label(xalign=0,ellipsize=3); self.path.set_hexpand(True); t.append(self.path); b=Gtk.Button.new_from_icon_name('go-up-symbolic'); b.connect('clicked',lambda x:self.setdir(self.ldir.parent)); t.append(b); b=Gtk.Button(label='Choose Folder…'); b.connect('clicked',self.choose); t.append(b); lb.append(t)
  self.ll=Gtk.ListBox(); self.ll.connect('row-selected',self.lselected); self.ll.connect('row-activated',self.lactivate); s=Gtk.ScrolledWindow(); s.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC); s.set_vexpand(True); s.set_child(self.ll); lb.append(s)
  t=Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,spacing=5); t.append(Gtk.Label(label='Drive')); self.dk=[None]+list(range(16)); self.dd=Gtk.DropDown(model=Gtk.StringList.new(['Current']+[f'{chr(65+i)}:' for i in range(16)])); self.dd.connect('notify::selected',self.target); t.append(self.dd); t.append(Gtk.Label(label='User')); self.uk=[None]+list(range(16)); self.ud=Gtk.DropDown(model=Gtk.StringList.new(['Current']+[str(i) for i in range(16)])); self.ud.connect('notify::selected',self.target); t.append(self.ud); b=Gtk.Button.new_from_icon_name('view-refresh-symbolic'); b.connect('clicked',self.crefresh); t.append(b); rb.append(t)
  self.cs=Gtk.Label(label='Run HOST.COM v2.1, then Refresh',xalign=0); rb.append(self.cs); self.cl=Gtk.ListBox(); self.cl.connect('row-selected',self.cselected); s=Gtk.ScrolledWindow(); s.set_policy(Gtk.PolicyType.NEVER,Gtk.PolicyType.AUTOMATIC); s.set_vexpand(True); s.set_child(self.cl); rb.append(s)
  self.dnd(); a=Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,spacing=7); a.set_halign(Gtk.Align.CENTER); self.sb=Gtk.Button(label='Send →'); self.sb.add_css_class('suggested-action'); self.sb.connect('clicked',self.send); a.append(self.sb); self.rb=Gtk.Button(label='← Receive'); self.rb.connect('clicked',self.recv); a.append(self.rb); self.cb=Gtk.Button(label='Cancel'); self.cb.connect('clicked',lambda x:self.cancel_ev.set()); a.append(self.cb); root.append(a)
  self.status=Gtk.Label(label='Ready',xalign=0); root.append(self.status); self.prog=Gtk.ProgressBar(); root.append(self.prog); lg=Adw.PreferencesGroup(title='Transfer log'); root.append(lg); fr=Gtk.Frame(); fr.set_size_request(-1,160); lg.add(fr); sc=Gtk.ScrolledWindow(); fr.set_child(sc); self.buf=Gtk.TextBuffer(); self.lv=Gtk.TextView(buffer=self.buf); self.lv.set_editable(False); self.lv.set_monospace(True); sc.set_child(self.lv); self.buttons()
 def pane(self,n):
  f=Gtk.Frame(); f.set_hexpand(True); f.set_vexpand(True); b=Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=6); [x(8) for x in (b.set_margin_top,b.set_margin_bottom,b.set_margin_start,b.set_margin_end)]; l=Gtk.Label(label=n,xalign=0); l.add_css_class('title-3'); b.append(l); f.set_child(b); return f,b
 def row(self,n,z,folder=False):
  r=Gtk.ListBoxRow(); b=Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,spacing=6); [x(5) for x in (b.set_margin_top,b.set_margin_bottom)]; b.append(Gtk.Image.new_from_icon_name('folder-symbolic' if folder else 'text-x-generic-symbolic')); l=Gtk.Label(label=n,xalign=0,ellipsize=3); l.set_hexpand(True); b.append(l); q=Gtk.Label(label=z); q.add_css_class('dim-label'); b.append(q); r.set_child(b); return r
 def clear(self,w):
  while (c:=w.get_first_child()) is not None:w.remove(c)
 def sz(self,n): return f'{n} B' if n<1024 else (f'{n/1024:.1f} KiB' if n<1048576 else f'{n/1048576:.2f} MiB')
 def lrefresh(self):
  self.path.set_text(str(self.ldir)); self.clear(self.ll); self.lrows={}; self.lsel=None
  try:e=sorted(self.ldir.iterdir(),key=lambda p:(not p.is_dir(),p.name.casefold()))
  except OSError as x:self.toast(x);return
  for p in e:
   r=self.row(p.name,'Folder' if p.is_dir() else self.sz(p.stat().st_size),p.is_dir()); self.ll.append(r); self.lrows[id(r)]=p
   if p.is_file():d=Gtk.DragSource();d.set_actions(Gdk.DragAction.COPY);d.connect('prepare',lambda s,x,y,p=str(p):self.provider(self.LD+p));r.add_controller(d)
  self.buttons()
 def setdir(self,p):
  p=Path(p)
  if p.is_dir():self.ldir=p;self.cfg['last_directory']=str(p);save_settings(self.cfg);self.lrefresh()
 def choose(self,_):
  d=Gtk.FileDialog();d.set_initial_folder(Gio.File.new_for_path(str(self.ldir)));d.select_folder(self,None,self.chosen)
 def chosen(self,d,r):
  try:f=d.select_folder_finish(r)
  except GLib.Error:return
  if f.get_path():self.setdir(f.get_path())
 def lselected(self,w,r):p=self.lrows.get(id(r)) if r else None;self.lsel=p if p and p.is_file() else None;self.buttons()
 def lactivate(self,w,r):
  p=self.lrows.get(id(r));self.setdir(p) if p and p.is_dir() else None
 def crender(self,fs):
  self.cfiles=list(fs);self.clear(self.cl);self.crows={};self.csel=None
  if not fs:r=self.row('No directory loaded','');r.set_sensitive(False);self.cl.append(r)
  for f in fs:
   r=self.row(f.name,self.sz(f.size_bytes));self.cl.append(r);self.crows[id(r)]=f;d=Gtk.DragSource();d.set_actions(Gdk.DragAction.COPY);d.connect('prepare',lambda s,x,y,n=f.name:self.provider(self.CD+n));r.add_controller(d)
  self.buttons()
 def cselected(self,w,r):self.csel=self.crows.get(id(r)) if r else None;self.buttons()
 def provider(self,s):return Gdk.ContentProvider.new_for_value(GObject.Value(str,s))
 def dnd(self):
  a=Gtk.DropTarget.new(str,Gdk.DragAction.COPY);a.connect('drop',self.dropl);self.ll.add_controller(a);a=Gtk.DropTarget.new(str,Gdk.DragAction.COPY);a.connect('drop',self.dropc);self.cl.add_controller(a)
 def dropc(self,t,v,x,y):
  if not isinstance(v,str) or not v.startswith(self.LD):return False
  p=Path(v[len(self.LD):]);self.lsel=p if p.is_file() else None;self.send();return bool(self.lsel)
 def dropl(self,t,v,x,y):
  if not isinstance(v,str) or not v.startswith(self.CD):return False
  self.csel=next((f for f in self.cfiles if f.name==v[len(self.CD):]),None);self.recv();return bool(self.csel)
 def refresh_ports(self):
  old=str(self.cfg.get('last_port',''));e=[(p.device,f'{p.device} — {p.description}') for p in list_ports.comports()] if list_ports else []
  if old and old not in [x[0] for x in e]:e.append((old,old+' — saved'))
  while self.pm.get_n_items():self.pm.remove(0)
  self.ports=[x[0] for x in e];[self.pm.append(x[1]) for x in e];self.pdd.set_selected(self.ports.index(old) if old in self.ports else (0 if self.ports else Gtk.INVALID_LIST_POSITION))
 def restore(self):
  b=str(self.cfg.get('baud',115200));self.br.set_selected(self.BAUD.index(b) if b in self.BAUD else 4);d=self.cfg.get('target_drive','current');u=self.cfg.get('target_user','current');self.dd.set_selected(0 if d=='current' else int(d)+1);self.ud.set_selected(0 if u=='current' else int(u)+1)
 def port(self):i=self.pdd.get_selected();return self.ports[i] if i!=Gtk.INVALID_LIST_POSITION and i<len(self.ports) else ''
 def drive(self):i=self.dd.get_selected();return self.dk[i] if i!=Gtk.INVALID_LIST_POSITION else None
 def user(self):i=self.ud.get_selected();return self.uk[i] if i!=Gtk.INVALID_LIST_POSITION else None
 def baud(self):return int(self.BAUD[self.br.get_selected()])
 def save(self,*_):
  if self.port():self.cfg['last_port']=self.port()
  self.cfg['baud']=self.baud();save_settings(self.cfg)
 def target(self,*_):
  d,u=self.drive(),self.user();self.cfg['target_drive']='current' if d is None else str(d);self.cfg['target_user']='current' if u is None else str(u);save_settings(self.cfg);self.crender([])
 def ser(self):
  k=dict(port=self.port(),baudrate=self.baud(),bytesize=serial.EIGHTBITS,parity=serial.PARITY_NONE,stopbits=serial.STOPBITS_ONE,timeout=.15,write_timeout=10,xonxoff=False,rtscts=False,dsrdtr=False)
  if os.name=='posix':k['exclusive']=True
  try:return serial.Serial(**k)
  except TypeError:k.pop('exclusive',None);return serial.Serial(**k)
 def begin(self,s):self.busy=True;self.cancel_ev.clear();self.prog.set_fraction(0);self.status.set_text(s);self.buttons()
 def link(self,ser):return HostLinkV2(ser,on_log=lambda s:GLib.idle_add(self.logui,s),on_progress=lambda d,t,s:GLib.idle_add(self.pg,d,t,s),cancel_event=self.cancel_ev)
 def crefresh(self,*_):
  if self.busy or not self.port():return
  self.begin('Reading CP/M directory…');self.worker=threading.Thread(target=self.cwork,daemon=True);self.worker.start()
 def cwork(self):
  try:
   with self.ser() as s:fs=self.link(s).request_directory(self.drive(),self.user());GLib.idle_add(self.cdone,fs)
  except Exception as e:GLib.idle_add(self.err,str(e))
 def cdone(self,fs):self.busy=False;self.crender(fs);self.cs.set_text(f'{len(fs)} file(s)');self.status.set_text('Directory received');self.buttons();return False
 def send(self,*_):
  if self.busy or not self.lsel or not self.port():return
  p=self.lsel;self.begin(f'Sending {p.name}…');self.worker=threading.Thread(target=self.swork,args=(p,),daemon=True);self.worker.start()
 def swork(self,p):
  try:
   with self.ser() as s:st=self.link(s).send_file(str(p),cpm_filename(str(p)),self.drive(),self.user());GLib.idle_add(self.done,st,'Sent')
  except Exception as e:GLib.idle_add(self.err,str(e))
 def recv(self,*_):
  if self.busy or not self.csel or not self.port():return
  f=self.csel;p=self.ldir/f.name;n=1
  while p.exists():p=self.ldir/f'{Path(f.name).stem}_{n}{Path(f.name).suffix}';n+=1
  self.begin(f'Receiving {f.name}…');self.worker=threading.Thread(target=self.rwork,args=(f,p),daemon=True);self.worker.start()
 def rwork(self,f,p):
  try:
   with self.ser() as s:st=self.link(s).receive_file(f.name,str(p),self.drive(),self.user(),f.size_bytes);GLib.idle_add(self.rdone,st,str(p))
  except Exception as e:GLib.idle_add(self.err,str(e))
 def done(self,st,verb):self.busy=False;self.prog.set_fraction(1);self.status.set_text(verb+' complete');self.log(f'{verb}: {st.bytes_in_file:,} bytes, {st.blocks_sent} blocks');self.buttons();GLib.timeout_add(350,self.after);return False
 def rdone(self,st,p):self.busy=False;self.prog.set_fraction(1);self.status.set_text('Receive complete');self.log(f'Received {st.bytes_in_file:,} bytes -> {p}');self.lrefresh();self.buttons();return False
 def after(self):
  if self.worker and self.worker.is_alive():return True
  self.crefresh();return False
 def pg(self,d,t,s):f=1 if not t else min(1,d/t);self.prog.set_fraction(f);self.status.set_text(f'{f*100:.1f}% — block {s.blocks_sent}');return False
 def err(self,e):self.busy=False;self.status.set_text('Operation failed');self.log('ERROR: '+e);self.toast(e);self.buttons();return False
 def buttons(self):
  if hasattr(self,'sb'):self.sb.set_sensitive(not self.busy and self.lsel is not None);self.rb.set_sensitive(not self.busy and self.csel is not None);self.cb.set_sensitive(self.busy)
 def log(self,s):self.buf.insert(self.buf.get_end_iter(),str(s).rstrip()+'\n')
 def logui(self,s):self.log(s);return False
 def toast(self,s):t=Adw.Toast.new(str(s));t.set_timeout(5);self.ov.add_toast(t)

class App(Adw.Application):
 def __init__(self):super().__init__(application_id=APP_ID);self.connect('activate',self.a)
 def a(self,*_):(self.get_active_window() or Win(self)).present()
if __name__=='__main__':Adw.init();raise SystemExit(App().run(None))
