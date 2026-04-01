import yaml
import os

class Config:
    def __init__(self, filename="config.yaml"):
        # Default to the parent directory's config.yaml if it exists
        if not os.path.exists(filename) and os.path.exists("../config.yaml"):
            filename = "../config.yaml"
            
        with open(filename, 'r', encoding='utf-8') as f:
            self.data = yaml.safe_load(f)
            
        self.log = self.data.get('log', {})
        self.auth = self.data.get('auth', {})
        self.mavlink = self.data.get('mavlink', {})
        self.forwarding = self.data.get('forwarding', {})
        self.web = self.data.get('web', {})

    def get_address(self):
        return f"{self.network.get('target_host')}:{self.network.get('target_port')}"
