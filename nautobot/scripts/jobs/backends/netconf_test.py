from ncclient import manager

# Define the router connection details
router = {
    "host": "192.168.31.21",
    "port": 830,
    "username": "airband",
    "password": "A1rband",
    "hostkey_verify": False,  # Disable strict host key verification
}

# Connect to the router
with manager.connect(**router) as m:
    print("NETCONF Session Established")

    # Get and print device capabilities
    for capability in m.server_capabilities:
        print(capability)
