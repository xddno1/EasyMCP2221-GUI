"""@author lichunjiang: GUI handoff protocol regression; no HID, reset or flash."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'workbench/Firmware_cooperation.py'


@unittest.skipUnless(os.name=='nt','Windows firmware cooperation protocol')
class CooperationTests(unittest.TestCase):
    """@author lichunjiang: Validate ownership and recovery contracts independently of Tk."""
    def setUp(self):
        """@author lichunjiang: Isolate every request and acknowledgement directory."""
        specification=importlib.util.spec_from_file_location('cooperation_test',SOURCE)
        self.module=importlib.util.module_from_spec(specification);specification.loader.exec_module(self.module)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT)
        self.directory=Path(self.temp.name)
        self.module.DIRECTORY=self.directory

    def tearDown(self):
        """@author lichunjiang: Remove only test-owned state files."""
        self.temp.cleanup()

    def test_acknowledgement_requires_matching_token(self):
        """@author lichunjiang: Old GUI state cannot authorize a new skill HID session."""
        m=self.module;lease=m.Lease(self.directory,[os.getpid()])
        m.publish(lease.path,lease.request)
        m.publish(self.directory/('gui-%d.json'%os.getpid()),{'protocol':1,'state':'paused','token':'old'})
        self.assertFalse(lease.paused())
        m.publish(self.directory/('gui-%d.json'%os.getpid()),{'protocol':1,'state':'paused','token':lease.request['token']})
        self.assertTrue(lease.paused())

    def test_another_live_owner_is_not_overwritten(self):
        """@author lichunjiang: Whole-operation ownership applies across workspace clients."""
        m=self.module;lease=m.Lease(self.directory,[])
        existing={'pid':os.getpid(),'token':'first'};m.publish(lease.path,existing)
        with self.assertRaisesRegex(m.FirmwareBusy,'Another firmware'):
            lease.acquire()
        self.assertEqual(m.read(lease.path),existing)

    def test_proxy_blocks_callbacks_before_device_calls(self):
        """@author lichunjiang: Queued button events cannot write after GUI acknowledged pause."""
        m=self.module;device=mock.Mock();bridge=mock.Mock(paused=True)
        proxy=m.DeviceProxy(device,bridge)
        with self.assertRaises(m.FirmwareBusy):proxy.GPIO_write(gp0=False)
        device.GPIO_write.assert_not_called()

    def test_startup_pause_ack_has_no_hid_initialization(self):
        """@author lichunjiang: A GUI opened mid-operation waits without device initialization."""
        m=self.module;app=mock.Mock(i2c_scanning=False)
        app.winfo_children.return_value=[]
        gui=m.GuiCooperation(app)
        m.publish(self.directory/'request.json',{'pid':os.getpid(),'token':'start'})
        self.assertTrue(gui.tick())
        self.assertIsNone(gui.device)
        self.assertEqual(m.read(gui.status_path)['state'],'paused')

    def test_inflight_scan_delays_ack_and_timeout_restores_controls(self):
        """@author lichunjiang: Never close a handle under an I2C worker or strand the UI."""
        m=self.module;gui=m.GuiCooperation(mock.Mock(i2c_scanning=True))
        gui.device=mock.Mock();gui.controls={'sentinel':'normal'}
        gui.enable_controls=mock.Mock()
        request=self.directory/'request.json';m.publish(request,{'pid':os.getpid(),'token':'scan'})
        self.assertTrue(gui.tick())
        gui.device.hidhandler.close.assert_not_called()
        self.assertNotEqual(m.read(gui.status_path)['state'],'paused')
        request.unlink();gui.tick()
        gui.enable_controls.assert_called_with(True)

    def test_resume_uses_actual_levels_and_no_reset(self):
        """@author lichunjiang: Refresh GPIO cache from actual output, not stale SRAM defaults."""
        m=self.module;app=mock.Mock();app.sts={key:[mock.Mock() for _ in range(4)] for key in ('func','in','out')}
        gui=m.GuiCooperation(app)
        device=mock.Mock();device.status={'GPIO':{}}
        settings=[0]*64;settings[0]=0x61;settings[22:26]=[16,16,16,17]
        device.send_cmd.return_value=settings;device.GPIO_read.return_value=(0,1,1,None)
        gui.device=device;gui.refresh()
        self.assertEqual(device.status['GPIO']['gp0'],0)
        app.sts['out'][0].set.assert_called_once_with(0)
        device.reset.assert_not_called();device.SRAM_config.assert_not_called()

    def test_failed_resume_closes_handle_before_future_handoff(self):
        """@author lichunjiang: A partial reopen must not leave an active handle behind an ACK."""
        m=self.module;gui=m.GuiCooperation(mock.Mock(i2c_scanning=False))
        gui.device=mock.Mock();gui.device.hidhandler.read.return_value=[]
        gui.hid_path=b'fake';gui.paused=True
        gui.refresh=mock.Mock(side_effect=RuntimeError('read failure'))
        with self.assertRaisesRegex(RuntimeError,'read failure'):gui.tick()
        gui.device.hidhandler.close.assert_called_once()
        self.assertTrue(gui.paused)


if __name__=='__main__':unittest.main()
