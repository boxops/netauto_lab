import requests
import json
import os
from pprint import pprint

requests.packages.urllib3.disable_warnings()


class AirOS4API:
    """
    SSH
    cat /etc/version
    cat /etc/board.info
    cat /tmp/system.cfg
    host example: 10.4.0.82
    """

    pass


class AirOS6API:
    """
    SSH
    cat /etc/version
    cat /etc/board.info
    cat /tmp/system.cfg
    host example: 172.24.3.131
    """

    pass


class AirOS8API:
    """
    SSH
    cat /etc/version
    cat /etc/board.info
    cat /tmp/system.cfg
    host example: 172.25.25.71
    """

    def __init__(self, ip, username, password, endpoint, verbose=False):
        """Initialize the AirOS8API with a list of IP addresses."""
        self.ip = ip
        self.username = username
        self.password = password
        self.endpoint = endpoint
        self.verbose = verbose
        self.base_url = f"https://{self.ip}"
        self.session = None
        self.response = None

    def login(self):
        """Perform login to the device at the given IP address."""
        try:
            response = self.session.post(
                url=self.base_url + "/api/auth",
                data={
                    "username": self.username,
                    "password": self.password,
                },
                verify=False,
                timeout=5,
            )
            response.raise_for_status()
            if self.verbose:
                print(f"Login response status code: {response.status_code}")
                pprint(f"Login response content: {response.content}")
            return response
        except requests.exceptions.RequestException as e:
            print(f"Error during login: {e}")
            return None

    def scrape_url(self):
        """Scrape the URL for the given IP address."""
        try:
            response = self.session.get(
                self.base_url + "/" + self.endpoint, verify=False
            )
            response.raise_for_status()
            if self.verbose:
                print(f"Scraping response: {response.json()}")
            return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error during scraping URL: {e}")
            return None

    def open(self):
        """Open a session for the API."""
        self.session = requests.Session()
        if self.verbose:
            print(f"Session opened for {self.ip}")

    def close(self):
        """Close the session for the API."""
        if self.session:
            self.session.close()
            if self.verbose:
                print(f"Session closed for {self.ip}")

    def save_to_json(self):
        """Save the response to a JSON file."""
        if self.response:
            with open(f"response.json", "w") as file:
                json.dump(self.response, file, indent=4)
            if self.verbose:
                print("Response saved to response.json")
        else:
            if self.verbose:
                print("No response to save.")

    def print_response(self):
        """Print the response to the console."""
        if self.response:
            print(self.response)
            print(json.dumps(self.response, indent=4))
        else:
            print("No response to print.")

    def run(self) -> None:
        """Run the scraping process for all IP addresses."""
        try:
            self.open()
            login_response = self.login()
            if not login_response or login_response.status_code != 200:
                print(f"Failed to login to {self.ip}")
                return
            self.response = self.scrape_url()
            if self.response:
                # self.save_to_json()
                return self.response
            else:
                print("Failed to scrape URL.")
        finally:
            self.close()


class AirOS10API:
    """
    SSH
    cat /etc/config.json | jq
    cat /etc/board.json | jq
    # show version ???
    host example: 10.3.0.171
    """

    pass


if __name__ == "__main__":
    endpoint_examples = [
        "arp.cgi",
        "brmacs.cgi?brmacs=y",
        "sroutes.cgi",
        "status.cgi",
        # the following don't seem to work on AirOS 8
        "getcfg.sh",
        "getboardinfo.sh",
        "sta.cgi",
        "ifstats.cgi",
        "iflist.cgi",
        "log.cgi",
    ]

    # from dotenv import load_dotenv

    # load_dotenv()

    scraper = AirOS8API(
        ip="172.21.81.11",
        username=os.environ.get("UBNT_USERNAME"),
        password=os.environ.get("UBNT_PASSWORD"),
        endpoint="status.cgi",
        # verbose=True,
    )
    response = scraper.run()
    # scraper.print_response()
    scraper.save_to_json()
