# Configuration for this Module

The configuration is tedious if only following UCL guidelines.

## Prerequisites
- python3
- F-Secure antivirus (avaliable on UCL software database, but is down at the moment)
- Cisco AnyConnect (MQTT data only available through UCL VPN)

## Usage
- go to this directory in terminal
- create virtual environment by "python3 -m venv ./env"
- activate venv by typing "source "./env/bin/activate"
- install dependencies by typing "pip install -r requirements.txt"
- execute the python script you want
- leave by typing "deactivate"

## Common Bugs

### Cisco Anyconnect says something like "Antivirus not Updated".

- This software only works with F-Secure antivirus installed. Check if the antivirus is on.

### I get weird results when trying to fetch from broker.

- Make sure you subscribe to the correct branch, listed out on the brief in Week 5.

### My python virtual environment cannot be activated by using the activate command.

- In shell, aliases cannot be overwritten by the activation script. Check your config files like ~/.zshrc and delete aliases to "python" commmand.

## References
https://swdb.ucl.ac.uk/?filter=anti%20virus

https://www.ucl.ac.uk/isd/services/get-connected/ucl-virtual-private-network-vpn

