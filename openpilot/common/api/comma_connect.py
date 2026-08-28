import os
from urllib.parse import urlsplit

from openpilot.common.api.base import BaseApi

API_HOST = os.getenv('API_HOST', 'https://api.commadotai.com')
CONNECT_HOST = os.getenv('CONNECT_HOST', 'https://connect.comma.ai').rstrip('/')
CONNECT_DOMAIN = urlsplit(CONNECT_HOST).netloc


class CommaConnectApi(BaseApi):
  def __init__(self, dongle_id):
    super().__init__(dongle_id, API_HOST)
    self.user_agent = "openpilot-"
