import io,logging,unittest
import logger
from hpack import Encoder
class LoggingPrivacyTests(unittest.TestCase):
 def test_http2_header_encoder_does_not_emit_api_keys(self):
  stream=io.StringIO();handler=logging.StreamHandler(stream);logging.getLogger().addHandler(handler)
  try:Encoder().encode([(b'apikey',b'runner-test-secret')])
  finally:logging.getLogger().removeHandler(handler)
  self.assertNotIn('runner-test-secret',stream.getvalue())
if __name__=='__main__':unittest.main()
