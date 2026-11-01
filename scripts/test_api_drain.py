import contextlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch


def load(name,relative):
    path=Path(__file__).resolve().parents[1]/relative
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(),str(path),'exec'),module.__dict__)
    return module


class DrainUnitTests(unittest.TestCase):
    def setUp(self):self.drain=load('api_drain_test','server/api_drain.py')

    def test_socket_table_counts_halfopen_and_active_but_not_other_ports(self):
        header='sl local_address rem_address st tx rx tr tm retr uid timeout inode\n'
        text=header+'0: 00000000:1387 00000000:0000 0A 0 0 0 0 0 0 123\n'+\
             '1: 0100007F:1387 0100007F:1234 03 0 0 0 0 0 0 124\n'+\
             '2: 0100007F:1387 0100007F:1235 06 0 0 0 0 0 0 125\n'+\
             '3: 0100007F:1388 0100007F:1236 01 0 0 0 0 0 0 126\n'
        entries=self.drain.socket_entries(text)
        self.assertEqual([entry[1]for entry in entries],['0A','03','06'])
        self.assertEqual(sum(state not in ('0A','06')for _,state,_ in entries),1)
        with self.assertRaises(self.drain.DrainError):self.drain.socket_entries('header\nnot a valid socket entry')

    def test_first_rule_and_exact_tag_are_required(self):
        d=self.drain;tag='teaching-api-drain-'+'a'*32
        good=('-P INPUT ACCEPT\n-A INPUT -m comment --comment '+tag+' -j REJECT\n').encode()
        with patch.object(d,'run',return_value=good),patch.object(d,'rule')as verify:
            d.require_first(d.IPTABLES[0],7,tag);verify.assert_called_once()
        bad=('-P INPUT ACCEPT\n-A INPUT -j ACCEPT\n-A INPUT -m comment --comment '+tag+' -j REJECT\n').encode()
        with patch.object(d,'run',return_value=bad):
            with self.assertRaisesRegex(d.DrainError,'API_DRAIN_RULE_NOT_FIRST'):d.require_first(d.IPTABLES[0],7,tag)

    def test_cleanup_checks_rule_list_before_and_after_removal(self):
        d=self.drain;tag='teaching-api-drain-'+'a'*32
        present=('-A INPUT -m comment --comment '+tag+' -j REJECT\n').encode()
        with patch.object(d,'run',side_effect=[present,b'']),patch.object(d,'rule')as command:
            d.remove_if_present(d.IPTABLES[0],7,tag)
            self.assertEqual([c.args[2]for c in command.call_args_list],['-C','-D'])
        with patch.object(d,'run',side_effect=d.DrainError('API_DRAIN_COMMAND_FAILED')):
            with self.assertRaises(d.DrainError):d.remove_if_present(d.IPTABLES[0],7,tag)

    def test_stop_request_is_inside_guard_lock_and_verification(self):
        d=self.drain;fence=d.Fence({});events=[]
        @contextlib.contextmanager
        def lock(_):events.append('locked');yield;events.append('unlocked')
        with patch.object(d,'guard_lock',lock),patch.object(fence,'verify',side_effect=lambda:events.append('verified')),\
             patch.object(d,'update_journal',side_effect=lambda _,phase:events.append(phase)),\
             patch.object(d,'read_journal',return_value={'tag':fence.tag,'phase':'stop_complete'}):
            fence.stop(lambda:events.append('confirmed'))
        self.assertEqual(events,['locked','verified','stop_requested','unlocked','confirmed'])

    def test_stop_cancellation_after_request_waits_for_confirmed_result(self):
        d=self.drain;events=[];calls=iter([None,ValueError('cancelled')])
        def check_cancel():
            value=next(calls,None)
            if value:raise value
        fence=d.Fence({},check_cancel=check_cancel)
        @contextlib.contextmanager
        def lock(_):yield
        with patch.object(d,'guard_lock',lock),patch.object(fence,'verify'),patch.object(d,'update_journal'),\
             patch.object(d,'marker_exists',return_value=False),patch.object(d,'write_marker',side_effect=lambda *args:events.append('cancel')),\
             patch.object(d,'read_journal',return_value={'tag':fence.tag,'phase':'stop_complete'}):
            with self.assertRaisesRegex(ValueError,'cancelled'):fence.stop(lambda:events.append('confirmed'))
        self.assertEqual(events,['cancel','confirmed'])

    def test_uncertain_stop_never_releases_the_fence(self):
        d=self.drain;fence=d.Fence({})
        @contextlib.contextmanager
        def lock(_):yield
        with patch.object(d,'guard_lock',lock),patch.object(fence,'verify'),patch.object(d,'update_journal'),\
             patch.object(d,'read_journal',return_value={'tag':fence.tag,'phase':'stop_uncertain'}):
            with self.assertRaisesRegex(d.DrainError,'API_DRAIN_STOP_UNCERTAIN'):fence.stop()
        self.assertTrue(fence.uncertain)

    def test_watchdog_executes_stop_once_and_marks_unknown_without_retry(self):
        d=self.drain;tag='teaching-api-drain-'+'a'*32
        @contextlib.contextmanager
        def lock(_):yield
        for scenario in ('success','timeout','crash','cancel','parent-dead','resumed-started'):
            with self.subTest(scenario=scenario):
                state={'tag':tag,'phase':'stop_started'if scenario=='resumed-started'else'stop_requested',
                       'parentPid':10,'parentStart':11,'expiresAt':10000,'containerId':'a'*64,
                       'image':'sha256:'+'b'*64,'startedAt':'original','forceExpiresAt':None}
                requests=[]
                def update(_,phase):state['phase']=phase
                def command(args,**kwargs):
                    if 'stop'in args:
                        requests.append('stop')
                        if scenario=='timeout':raise TimeoutError('unknown daemon outcome')
                        if scenario=='crash':raise KeyboardInterrupt()
                        return b''
                    return json.dumps([{'Id':state['containerId'],'Image':state['image'],'State':{
                        'StartedAt':'original','Running':False,'ExitCode':0,'OOMKilled':False,'Restarting':False}}]).encode()
                with patch.object(d,'guard_lock',lock),patch.object(d,'read_journal',side_effect=lambda:dict(state)),\
                     patch.object(d,'update_journal',side_effect=update),patch.object(d,'run',side_effect=command),\
                     patch.object(d,'marker_exists',side_effect=lambda _,suffix:scenario=='cancel'and suffix=='cancel'),\
                     patch.object(d,'process_alive',return_value=scenario!='parent-dead'),patch.object(d.time,'monotonic',return_value=1):
                    if scenario=='crash':
                        with self.assertRaises(KeyboardInterrupt):d.advance_stop(tag)
                        self.assertEqual(state['phase'],'stop_started');d.advance_stop(tag)
                    else:d.advance_stop(tag)
                    expected={'success':'stop_complete','timeout':'stop_uncertain','crash':'stop_uncertain',
                              'cancel':'stop_cancelled','parent-dead':'stop_cancelled','resumed-started':'stop_uncertain'}
                    self.assertEqual(state['phase'],expected[scenario]);d.advance_stop(tag)
                self.assertEqual(len(requests),1 if scenario in ('success','timeout','crash')else 0)

    def test_force_stop_requires_authorization_and_only_accepts_the_exact_sigkill_exit(self):
        d=self.drain;tag='teaching-api-drain-'+'a'*32
        @contextlib.contextmanager
        def lock(_):yield
        for scenario in ('forced','wrong_exit','oom','expired','expires_before_kill','normal_nonzero',
                         'changed_pid','changed_start','changed_image','changed_namespace',
                         'post_changed_start','post_changed_image'):
            state={'tag':tag,'phase':'stop_requested','parentPid':10,'parentStart':11,'expiresAt':10000,
                   'containerId':'a'*64,'image':'sha256:'+'b'*64,'startedAt':'original',
                   'pid':777,'namespaceInode':888,
                   'forceExpiresAt':None if scenario=='normal_nonzero' else (1 if scenario=='expired' else 200)}
            requests=[];inspects=[]
            def command(args,**kwargs):
                if 'kill' in args or 'stop' in args:
                    requests.append(args);return b''
                if state['forceExpiresAt'] is not None and not inspects:
                    inspects.append(1)
                    return json.dumps([{'Id':state['containerId'],'Image':'sha256:'+'e'*64 if scenario=='changed_image' else state['image'],
                        'State':{'StartedAt':'changed' if scenario=='changed_start' else 'original',
                                 'Running':True,'Pid':778 if scenario=='changed_pid' else 777,'Restarting':False}}]).encode()
                return json.dumps([{'Id':state['containerId'],'Image':'sha256:'+'e'*64 if scenario=='post_changed_image' else state['image'],'State':{
                    'StartedAt':'changed' if scenario=='post_changed_start' else 'original','Running':False,'ExitCode':0 if scenario=='wrong_exit' else 137,
                    'OOMKilled':scenario=='oom','Restarting':False}}]).encode()
            with self.subTest(scenario=scenario),patch.object(d,'guard_lock',lock),\
                 patch.object(d,'read_journal',side_effect=lambda:dict(state)),\
                 patch.object(d,'update_journal',side_effect=lambda _,phase:state.update(phase=phase)),\
                 patch.object(d,'run',side_effect=command),patch.object(d,'marker_exists',return_value=False),\
                 patch.object(d,'namespace_inode',return_value=889 if scenario=='changed_namespace' else 888),\
                 patch.object(d,'process_alive',return_value=True),patch.object(d.time,'monotonic',return_value=1),\
                 patch.object(d.time,'time',side_effect=[100,201] if scenario=='expires_before_kill' else None,return_value=100):
                d.advance_stop(tag)
            self.assertEqual(state['phase'],{'forced':'stop_complete','wrong_exit':'stop_uncertain',
                'oom':'stop_uncertain','expired':'stop_cancelled','expires_before_kill':'stop_cancelled',
                'normal_nonzero':'stop_uncertain','changed_pid':'stop_uncertain','changed_start':'stop_uncertain',
                'changed_image':'stop_uncertain','changed_namespace':'stop_uncertain',
                'post_changed_start':'stop_uncertain','post_changed_image':'stop_uncertain'}[scenario])
            self.assertEqual(len(requests),0 if scenario in ('expired','expires_before_kill','changed_pid',
                'changed_start','changed_image','changed_namespace') else 1)
            if scenario in ('forced','wrong_exit','oom','post_changed_start','post_changed_image'):self.assertIn('kill',requests[0])
            if scenario=='normal_nonzero':self.assertIn('stop',requests[0])

    @unittest.skipUnless(os.geteuid()==0 and os.path.exists('/proc/self/ns/net'),'requires Linux root')
    def test_authorized_force_installs_dual_fence_without_waiting_for_connections(self):
        d=self.drain;events=[]
        api={'State':{'Pid':os.getpid()}}
        fence=d.Fence(api,force_expires_at=int(time.time())+120)
        @contextlib.contextmanager
        def lock(_):yield
        def command(args,**kwargs):
            if args==['/usr/bin/uname','-r']:return (d.VERIFIED_KERNEL+'\n').encode()
            if args[:2]==[d.SYSTEMCTL,'show']:return b'not-found'
            if args[0]==d.SYSTEMD_RUN:return b''
            raise AssertionError(args)
        with tempfile.TemporaryDirectory()as directory:
            with patch.object(d,'RUN',Path(directory)),patch.object(fence,'identity'),\
                 patch.object(fence,'sockets',side_effect=lambda:events.append('existing-connection')or 2),\
                 patch.object(fence,'idle',side_effect=AssertionError('force must not wait for connections')),\
                 patch.object(fence,'verify_watcher'),patch.object(fence,'verify'),patch.object(fence,'close'),\
                 patch.object(d,'run',side_effect=command),patch.object(d,'require_no_server_fastopen'),\
                 patch.object(d,'create_journal'),patch.object(d,'read_journal',return_value={'expiresAt':time.monotonic()+300}),\
                 patch.object(d,'guard_lock',lock),patch.object(d,'rule',side_effect=lambda binary,*_:events.append(binary)):
                try:fence.__enter__()
                finally:
                    if fence.fd is not None:os.close(fence.fd)
        self.assertEqual(events,['existing-connection',d.IPTABLES[0],d.IPTABLES[1],'existing-connection'])

    def test_forced_queue_check_occurs_after_fence_and_before_stop(self):
        receiver=load('receiver_force_order_test','server/tencent_receiver.py');events=[]
        class FakeFence:
            def __init__(self,*args,**kwargs):
                self.expires=kwargs['force_expires_at']
            def __enter__(self):events.append(('fenced',self.expires));return self
            def __exit__(self,*args):events.append('closed')
            def stop(self,callback):events.append('stop');callback()
        helper=types.SimpleNamespace(Fence=FakeFence,DrainError=ValueError)
        with patch.object(receiver,'api_drain_module',return_value=helper):
            receiver.stop_api_guarded({},lambda:events.append('confirmed'),1234,lambda:events.append('queue-idle'))
        self.assertEqual(events,[('fenced',1234),'queue-idle','stop','confirmed','closed'])

    def test_forced_queue_refusal_never_requests_stop(self):
        receiver=load('receiver_force_busy_test','server/tencent_receiver.py');events=[]
        class FakeFence:
            def __init__(self,*args,**kwargs):pass
            def __enter__(self):events.append('fenced');return self
            def __exit__(self,*args):events.append('closed')
            def stop(self,callback):events.append('unsafe-stop')
        def busy():raise receiver.Refused('FORCE_RESUME_ACTIVE_WORK')
        with patch.object(receiver,'api_drain_module',return_value=types.SimpleNamespace(Fence=FakeFence,DrainError=ValueError)):
            with self.assertRaisesRegex(receiver.Refused,'FORCE_RESUME_ACTIVE_WORK'):
                receiver.stop_api_guarded({},force_expires_at=1234,pre_stop=busy,on_request=lambda:events.append('request'))
        self.assertEqual(events,['fenced','closed'])

    def test_exception_after_stop_request_never_releases_unknown_guard(self):
        d=self.drain;fence=d.Fence({});events=[]
        @contextlib.contextmanager
        def lock(_):yield
        with patch.object(d,'guard_lock',lock),patch.object(fence,'verify'),patch.object(d,'update_journal'),\
             patch.object(d,'marker_exists',return_value=False),patch.object(d,'write_marker',side_effect=lambda *args:events.append(args[-1])),\
             patch.object(d,'read_journal',side_effect=OSError('I/O unknown')):
            with self.assertRaisesRegex(d.DrainError,'API_DRAIN_STOP_UNCERTAIN'):fence.stop()
        self.assertTrue(fence.uncertain);self.assertEqual(events,['cancel'])

    def test_stop_request_fsync_failure_is_unknown_and_requests_cancel(self):
        d=self.drain;fence=d.Fence({});events=[]
        @contextlib.contextmanager
        def lock(_):yield
        with patch.object(d,'guard_lock',lock),patch.object(fence,'verify'),\
             patch.object(d,'update_journal',side_effect=OSError('fsync status unknown')),\
             patch.object(d,'marker_exists',return_value=False),patch.object(d,'write_marker',side_effect=lambda *args:events.append(args[-1])):
            with self.assertRaisesRegex(d.DrainError,'API_DRAIN_STOP_UNCERTAIN'):fence.stop()
        self.assertTrue(fence.uncertain);self.assertEqual(events,['cancel'])

    def test_receiver_never_stops_api_when_fence_refuses(self):
        receiver=load('receiver_drain_test','server/tencent_receiver.py');events=[]
        class FailedFence:
            def __init__(self,*args,**kwargs):pass
            def __enter__(self):raise ValueError('API_DRAIN_CONNECTION_TIMEOUT')
            def __exit__(self,*args):pass
        helper=types.SimpleNamespace(Fence=FailedFence,DrainError=ValueError)
        with patch.object(receiver,'api_drain_module',return_value=helper),patch.object(receiver,'stop_api_for_release',side_effect=lambda _:events.append('stop')):
            with self.assertRaisesRegex(receiver.Refused,'API_DRAIN_CONNECTION_TIMEOUT'):receiver.stop_api_guarded({})
        self.assertEqual(events,[])

    def test_install_marker_blocks_receiver_including_diagnostics_and_symlinks(self):
        receiver=load('receiver_marker_test','server/tencent_receiver.py')
        with tempfile.TemporaryDirectory()as directory:
            receiver.STATE=Path(directory);path=receiver.STATE/'api-drain-install.pending.json'
            path.symlink_to(receiver.STATE/'missing')
            for diagnostic in (False,True):
                with self.assertRaisesRegex(receiver.Refused,'API_DRAIN_INSTALLATION_RECOVERY_REQUIRED'):
                    receiver.require_host_ready(allow_diagnostics=diagnostic)
            path.unlink();path.write_text('{}')
            with self.assertRaisesRegex(receiver.Refused,'API_DRAIN_INSTALLATION_RECOVERY_REQUIRED'):receiver.require_host_ready()


if __name__=='__main__':unittest.main()
