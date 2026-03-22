import http.client
import json
import ssl
from json import JSONDecodeError


class Tachyon:
    def __init__(
        self,
        job,
        ip,
        username,
        password,
        verbose=False,
        use_https=True,
        verify_ssl=False,
    ):
        self.job = job
        self.ip = ip
        self.username = username
        self.password = password
        self.verbose = verbose
        self.use_https = use_https
        self.verify_ssl = verify_ssl
        self.token = None

    def _connect(self):
        if self.use_https:
            if not self.verify_ssl:
                # Create an SSL context that doesn't verify certificates
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                return http.client.HTTPSConnection(self.ip, context=context)
            else:
                return http.client.HTTPSConnection(self.ip)
        else:
            return http.client.HTTPConnection(self.ip)

    def login(self):
        self.job.logger.info(
            f"Attempting to login with username {self.username} and password {self.password} to IP {self.ip}\n"
        )
        headers = {"Content-Type": "application/json"}
        login_data = json.dumps({"username": self.username, "password": self.password})

        try:
            conn = self._connect()
            conn.request("POST", "/cgi.lua/apiv1/login", login_data, headers=headers)
            response = conn.getresponse()
            response_body = json.loads(response.read().decode())
            self.job.logger.info(f"Response from login: {response_body}\n")
            if "error" in response_body:
                self.job.logger.error(
                    "Error logging in: " + response_body["error"]["details"]
                )
                conn.close()
                raise Exception(f"Login failed: {response_body['error']['details']}")
            self.token = response_body["token"]
            self.job.logger.info(f"Token is {self.token}")
            conn.close()
            return self.token
        except Exception as e:
            self.job.logger.error(f"Error connecting to Tachyon device: {e}")
            raise

    def logout(self):
        if not self.token:
            return
        headers = {"Cookie": f"api_token={self.token}"}
        conn = self._connect()
        conn.request("DELETE", "/cgi.lua/apiv1/login", headers=headers)
        conn.getresponse().read()
        conn.close()
        self.token = None

    def get_config(self):
        if not self.token:
            raise Exception("Not logged in")
        self.job.logger.info(
            f"Fetching config from device {self.ip} with token {self.token}\n"
        )
        headers = {
            "Content-Type": "application/json",
            "Cookie": f"api_token={self.token}",
        }
        conn = self._connect()
        conn.request("GET", "/cgi.lua/apiv1/config", headers=headers)
        response = conn.getresponse()
        response_body = json.loads(response.read().decode())
        self.job.logger.info(f"Response from fetch_config: {response_body}\n")
        conn.close()
        return response_body

    def get_stats(self):
        if not self.token:
            raise Exception("Not logged in")
        headers = {
            "Content-Type": "application/json",
            "Cookie": f"api_token={self.token}",
        }
        conn = self._connect()
        conn.request(
            "GET", "/cgi.lua/apiv1/stats?type=system,wireless", headers=headers
        )
        response = conn.getresponse()
        response_body = json.loads(response.read().decode())
        self.job.logger.info(f"Response from get_stats: {response_body}\n")
        conn.close()
        return response_body

    def set_hostname(self, config, hostname):
        config["system"]["hostname"] = hostname
        return config

    def push_config(self, config, dry_run=False):
        if not self.token:
            raise Exception("Not logged in")
        data = {"config": config, "dry_run": dry_run}
        str_data = json.dumps(data)
        headers = {
            "Content-Type": "application/json",
            "Cookie": f"api_token={self.token}",
            "Content-Length": len(str_data),
        }
        self.job.logger.info(f"Pushing new config now, dry run is set to {dry_run}.\n")
        conn = self._connect()
        conn.request("POST", "/cgi.lua/apiv1/config", str_data, headers=headers)
        response = conn.getresponse()
        self.job.logger.info(
            f"Response code is: {response.status}, reason: {response.reason}"
        )
        response_body = response.read().decode()
        self.job.logger.info(f"Response body is: {response_body}")
        valid = True
        try:
            json_response = json.loads(response_body)
        except (JSONDecodeError, json.JSONDecodeError):
            valid = False
        if not valid:
            print("Error: received invalid JSON response")
            conn.close()
            return
        if "error" in json_response:
            print(
                "Received config response error: " + json_response["error"]["details"]
            )
        elif response.status != 200:
            print("Received server error: " + response_body)
        else:
            print(f"Config change response: {json_response['status_msg']}")
            print(
                f"\tIs reboot required? {json_response['response']['reboot_required']}"
            )
            print(f"\tKeys changed: {json_response['response']['keys_changed']}")
            print(f"\tKeys added: {json_response['response']['keys_added']}")
            print(f"\tKeys removed: {json_response['response']['keys_removed']}")
            print(f"\tWarnings: {json_response['response']['warnings']}")
        conn.close()

    def change_hostname(self, new_hostname, dry_run=False):
        self.login()
        response_body = self.get_config()
        if "error" in response_body:
            print("Error fetching config: " + response_body["error"]["details"])
            self.logout()
            exit()
        config = response_body["config"]
        config = self.set_hostname(config, new_hostname)
        self.push_config(config, dry_run=dry_run)
        self.logout()
