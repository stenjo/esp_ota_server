# esp_ota_server
Python based server with auth for ota update of python files

## Setup

### Authentication

Server needs authentication 
Create file `.ota_credentials`preferably in user root:

```
echo "admin;yourpassword" > ~/.ota_credentials
chmod 600 ~/.ota_credentials

```

### Projects to provide

Update or modify `.ota_projects.json` file, initially seet to:

```json
{
  "esp-temp-and-pressure": "stenjo/esp-temp-and-pressure",
  "another-project": "youruser/another-repo"
}
```
Consider moving ths to user root

## Start server

```bash
python3 ota_github_server.py
```

## Commands

http://<ip>:8000/sync_now?project=esp-temp-and-pressure
http://<ip>:8000/rollback?project=esp-temp-and-pressure


## Service setup for auto start

Copy the file `ota-server.service` file to `/etc/systemd/system/` folder on your raspberry pi
If neccessary, change the content: 

```bash
[Unit]
Description=Python OTA GitHub Server
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/esp_ota_server/ota_github_server.py
WorkingDirectory=/home/pi/esp_ota_server
Restart=always
User=pi
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Then initialize and start the service:

```bash
sudo systemctl daemon-reexec
sudo systemctl daemon-reload
sudo systemctl enable ota-server
sudo systemctl start ota-server
```
Verify that the service is running:

```bash
sudo systemctl status ota-server
```

