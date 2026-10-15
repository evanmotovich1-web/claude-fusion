"""Pi failures arrive in JSON even when its process exits zero."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import fusion

class PiProviderErrorTest(unittest.TestCase):
    def test_error_is_reported_instead_of_unrelated_stderr_warnings(self):
        slot=fusion.Slot('argon','pi',model='google-gemini-cli/gemini-4-argon')
        def fixture_stream(cmd,prompt,cwd,on_line):
            on_line(json.dumps({'type':'message_end','message':{'role':'assistant','content':[], 'stopReason':'error','errorMessage':'Google rejected this client (403): invalid license.'}}))
            return 0,'Warning: duplicate extension and host dependencies'
        with patch.object(fusion,'stream',fixture_stream):
            text,ok,_=fusion._pi(slot,'test','.',False,True,'')
        self.assertFalse(ok)
        self.assertIn('Google rejected this client (403): invalid license.',text)
        self.assertNotIn('duplicate extension',text)

    def test_partial_text_followed_by_error_is_not_success(self):
        slot=fusion.Slot('argon','pi',model='google-gemini-cli/gemini-4-argon')
        def fixture_stream(cmd,prompt,cwd,on_line):
            on_line(json.dumps({'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':'I will check'}],'stopReason':'toolUse'}}))
            on_line(json.dumps({'type':'message_end','message':{'role':'assistant','content':[],'stopReason':'error','errorMessage':'provider connection lost'}}))
            return 0,''
        with patch.object(fusion,'stream',fixture_stream):
            text,ok,_=fusion._pi(slot,'test','.',False,True,'')
        self.assertFalse(ok)
        self.assertIn('provider connection lost',text)

if __name__=='__main__': unittest.main()
