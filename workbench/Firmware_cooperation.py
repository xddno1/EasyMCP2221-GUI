"""@author lichunjiang: Cooperative HID handoff; GUI closes its handle before ACK."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import time
import uuid

PROTOCOL = 1
DIRECTORY = Path(__file__).resolve().parent / '.firmware-control'


class FirmwareBusy(RuntimeError):
    """@author lichunjiang: Reject GUI hardware callbacks while a skill owns the device."""


def alive(pid):
    """@author lichunjiang: Detect an owner crash without depending on GUI timers."""
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x1000,False,int(pid))
    if not handle:return ctypes.get_last_error()==5  # Access denied is not proof of death.
    try:
        code=wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
    finally:kernel.CloseHandle(handle)


def read(path):
    """@author lichunjiang: Read only complete atomically published state files."""
    try:return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:return None


def publish(path,value):
    """@author lichunjiang: Atomically update a GUI acknowledgement."""
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(value),encoding='utf-8')
        os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()


class Lease:
    """@author lichunjiang: Request exclusive skill ownership across workspaces."""
    def __init__(self,directory,gui_pids,timeout=10):
        """@author lichunjiang: Bind expected GUI process acknowledgements."""
        self.directory=Path(directory);self.path=self.directory/'request.json'
        self.gui_pids=list(gui_pids);self.timeout=timeout
        self.request={'protocol':PROTOCOL,'pid':os.getpid(),'token':uuid.uuid4().hex}
        self.owned=False

    def acquire(self):
        """@author lichunjiang: Publish once and wait until every GUI HID handle is closed."""
        self.directory.mkdir(exist_ok=True)
        existing=read(self.path)
        if existing:
            if alive(existing['pid']):raise FirmwareBusy('Another firmware operation owns MCP2221')
            self.path.unlink()
        temporary=self.path.with_name('request.'+self.request['token']+'.tmp')
        try:
            temporary.write_text(json.dumps(self.request),encoding='utf-8')
            os.link(temporary,self.path)  # Atomic create-if-absent, never overwrite another owner.
            self.owned=True
        finally:
            if temporary.exists():temporary.unlink()
        deadline=time.monotonic()+self.timeout
        try:
            while True:
                if self.paused():return self
                if time.monotonic()>=deadline:raise FirmwareBusy('EasyMCP pause acknowledgement timed out; HID not opened')
                time.sleep(.05)
        except BaseException:
            self.release()
            raise

    def paused(self):
        """@author lichunjiang: Reject stale/foreign acknowledgements, including PID reuse."""
        if read(self.path)!=self.request:return False
        for pid in self.gui_pids:
            if not alive(pid):continue
            status=read(self.directory/('gui-%d.json'%pid))
            if not status or status.get('protocol')!=PROTOCOL or status.get('state')!='paused' or status.get('token')!=self.request['token']:
                return False
        return True

    def validate(self):
        """@author lichunjiang: Prove cooperative GUI ownership before opening skill HID."""
        if not self.owned or not self.paused():raise FirmwareBusy('EasyMCP handoff is not confirmed')

    def release(self):
        """@author lichunjiang: Remove only this lease and let GUI reopen without reset."""
        if self.owned and read(self.path)==self.request:self.path.unlink()
        self.owned=False
        deadline=time.monotonic()+self.timeout
        while time.monotonic()<deadline:
            waiting=False
            for pid in self.gui_pids:
                status=read(self.directory/('gui-%d.json'%pid))
                if alive(pid) and status and status.get('state')=='paused' and status.get('token')==self.request['token']:
                    waiting=True
            if not waiting:return
            time.sleep(.05)
        raise FirmwareBusy('EasyMCP resume is not confirmed; check its window')


class DeviceProxy:
    """@author lichunjiang: Keep existing widgets bound while preventing paused callbacks."""
    def __init__(self,device,cooperation):
        """@author lichunjiang: Retain the one original device instance."""
        self.device=device;self.cooperation=cooperation

    def __getattr__(self,name):
        """@author lichunjiang: Guard methods before even GUI-side library state changes."""
        value=getattr(self.device,name)
        if not callable(value):return value
        def guarded(*args,**kwargs):
            """@author lichunjiang: Paused GUI events never send HID commands."""
            if self.cooperation.paused:raise FirmwareBusy('Firmware owns MCP2221; GUI controls are paused')
            return value(*args,**kwargs)
        return guarded


class GuiCooperation:
    """@author lichunjiang: Tk-thread handoff, including delayed GUI startup and crash recovery."""
    def __init__(self,app):
        """@author lichunjiang: Register capability before the GUI opens any HID handle."""
        self.app=app;self.device=None;self.hid_path=None;self.paused=False
        self.controls={};self.last=None
        DIRECTORY.mkdir(exist_ok=True)
        self.status_path=DIRECTORY/('gui-%d.json'%os.getpid())
        self.status('idle',None)

    def status(self,state,token):
        """@author lichunjiang: Publish only transitions, no continuous disk polling writes."""
        if (state,token)!=self.last:
            publish(self.status_path,{'protocol':PROTOCOL,'pid':os.getpid(),'state':state,'token':token})
            self.last=(state,token)

    def attach(self,device):
        """@author lichunjiang: Record the existing HID identity, without a device reset."""
        import hid
        devices=hid.enumerate(device.parm_VID,device.parm_PID)
        if len(devices)!=1:raise RuntimeError('MCP device identity is ambiguous')
        self.device=device;self.hid_path=devices[0]['path']
        return DeviceProxy(device,self)

    def enable_controls(self,enabled):
        """@author lichunjiang: Preserve original widget states across temporary ownership."""
        def visit(widget):
            """@author lichunjiang: Disable hardware controls while leaving the window responsive."""
            for child in widget.winfo_children():
                try:
                    if 'state' in child.keys():
                        if not enabled:
                            self.controls.setdefault(child,str(child.cget('state')))
                            child.configure(state='disabled')
                        elif child in self.controls:child.configure(state=self.controls.pop(child))
                except Exception:pass
                visit(child)
        visit(self.app)

    def refresh(self):
        """@author lichunjiang: Refresh live SRAM/cache/output values; never reload flash defaults."""
        device=self.device
        settings=device.send_cmd([0x61]);levels=device.GPIO_read()
        if settings[0]!=0x61:raise RuntimeError('MCP SRAM response mismatch on resume')
        names={0:{1:'SSPND',2:'LED_URX'},1:{1:'CLK_OUT',2:'ADC',3:'LED_UTX',4:'IOC'},
               2:{1:'USBCFG',2:'ADC',3:'DAC'},3:{1:'LED_I2C',2:'ADC',3:'DAC'}}
        for pin,setting in enumerate(settings[22:26]):
            function=setting&7
            if function==0:
                setting=(setting&~16)|(16 if levels[pin] else 0)
                name='GPIO_IN' if setting&8 else 'GPIO_OUT'
            else:name=names[pin].get(function)
            device.status['GPIO']['gp%d'%pin]=setting
            if name:self.app.sts['func'][pin].set(name)
            if levels[pin] is not None:
                self.app.sts['out'][pin].set(int(levels[pin]))
                self.app.sts['in'][pin].set(int(levels[pin]))
        device.status['dac_ref']=(settings[6]>>5)&7
        device.status['dac_value']=settings[6]&31
        device.status['adc_ref']=(settings[7]>>2)&7

    def tick(self):
        """@author lichunjiang: ACK only after a complete GUI operation and HID close."""
        request=read(DIRECTORY/'request.json')
        if request and not alive(request['pid']):request=None
        if request:
            if not self.paused:
                self.enable_controls(False)
                if self.app.i2c_scanning:return True
                if self.device:self.device.hidhandler.close()
                self.paused=True
            self.app.title('EasyMCP2221 Workbench - Firmware using hardware (paused)')
            self.status('paused',request['token'])
            return True
        if self.paused:
            if self.device:
                try:
                    self.device.hidhandler.open_path(self.hid_path)
                    for _ in range(32):
                        if not self.device.hidhandler.read(64,10):break
                    self.refresh()
                except BaseException:
                    self.device.hidhandler.close()
                    raise
            self.app.update_idletasks()
            self.paused=False
        if self.controls:
            self.enable_controls(True)
        self.app.title('EasyMCP2221 Workbench - Ready')
        self.status('idle',None)
        return False

    def close(self):
        """@author lichunjiang: Remove capability state only after closing the GUI handle."""
        if self.device and not self.paused:self.device.hidhandler.close()
        if self.status_path.exists():self.status_path.unlink()
