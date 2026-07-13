#!/usr/bin/env python3
"""
Script to set 4G network mode using AT commands
"""
import serial
import sys
import time
import logging

# Send logs to stderr to keep stdout clean for JSON
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

def find_usb_port():
    """Find the correct USB serial port for AT commands"""
    possible_ports = ['/dev/ttyUSB2', '/dev/ttyUSB1', '/dev/ttyUSB0']
    for port in possible_ports:
        try:
            ser = serial.Serial(port, 115200, timeout=2)
            ser.write(b'AT\r\n')
            time.sleep(0.2)
            response = ser.read(ser.in_waiting or 1).decode('utf-8', errors='ignore')
            ser.close()
            if 'OK' in response:
                return port
        except Exception:
            continue
    return None

def send_at_command(port, command, timeout=3):
    """Send AT command and return response"""
    ser = None
    try:
        ser = serial.Serial(port, 115200, timeout=timeout)
        time.sleep(0.1)
        
        # Clear buffer
        ser.read(ser.in_waiting or 1)
        
        # Send command
        cmd = command.strip() + '\r\n'
        ser.write(cmd.encode())
        time.sleep(0.5)
        
        # Read response
        response = ""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if ser.in_waiting:
                chunk = ser.read(ser.in_waiting).decode('utf-8', errors='ignore')
                response += chunk
                if 'OK' in response or 'ERROR' in response:
                    break
            time.sleep(0.1)
        
        return response.strip()
    except Exception as e:
        logger.error(f"Error sending AT command: {e}")
        return None
    finally:
        if ser and ser.is_open:
            ser.close()

def get_current_mode(port):
    """Get current network mode"""
    response = send_at_command(port, 'AT+CNMP?')
    if response and 'CNMP:' in response:
        # Parse mode from response like "+CNMP: 2"
        for line in response.split('\n'):
            if '+CNMP:' in line:
                try:
                    mode = int(line.split(':')[1].strip())
                    mode_names = {
                        2: "Automatic",
                        13: "GSM only (2G)",
                        14: "WCDMA only (3G)",
                        38: "LTE only (4G)",
                        51: "GSM and LTE (2G/4G)",
                        71: "GSM, WCDMA and LTE (2G/3G/4G)"
                    }
                    return {
                        "success": True,
                        "mode": mode,
                        "mode_name": mode_names.get(mode, f"Unknown mode {mode}")
                    }
                except:
                    pass
    return {"success": False, "message": "Failed to get current mode"}

def set_network_mode(port, mode):
    """
    Set network mode
    Modes:
    - 2: Automatic
    - 13: GSM only (2G)
    - 14: WCDMA only (3G)
    - 38: LTE only (4G)
    - 51: GSM and LTE (2G/4G)
    - 71: GSM, WCDMA and LTE (2G/3G/4G)
    """
    valid_modes = [2, 13, 14, 38, 51, 71]
    if mode not in valid_modes:
        return {
            "success": False,
            "message": f"Invalid mode {mode}. Valid modes: {valid_modes}"
        }
    
    mode_names = {
        2: "Automatic",
        13: "GSM only (2G)",
        14: "WCDMA only (3G)",
        38: "LTE only (4G)",
        51: "GSM and LTE (2G/4G)",
        71: "GSM, WCDMA and LTE (2G/3G/4G)"
    }
    
    logger.info(f"Setting network mode to: {mode_names[mode]}")
    
    # Set mode
    response = send_at_command(port, f'AT+CNMP={mode}', timeout=5)
    
    if not response:
        return {
            "success": False,
            "message": "No response from module"
        }
    
    if 'OK' in response:
        logger.info("Network mode set successfully")
        
        # Note: Changes will apply on next connection or module restart
        # No need to restart module immediately to avoid long delays
        
        return {
            "success": True,
            "message": f"Network mode set to {mode_names[mode]}. Changes will apply on next reconnection.",
            "mode": mode,
            "mode_name": mode_names[mode]
        }
    elif 'ERROR' in response or 'CME ERROR' in response:
        # Check if it's an unsupported band error
        if 'CME ERROR: 3' in response or 'not supported' in response.lower():
            return {
                "success": False,
                "message": "Băng tần không khả dụng (Band not available)",
                "error_type": "unsupported_band"
            }
        else:
            return {
                "success": False,
                "message": f"Module returned error: {response}",
                "error_type": "module_error"
            }
    else:
        return {
            "success": False,
            "message": f"Unexpected response: {response}"
        }

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 set_4g_mode.py <get|set> [mode]")
        print("Modes: 2=Auto, 13=2G, 14=3G, 38=4G, 51=2G/4G, 71=2G/3G/4G")
        sys.exit(1)
    
    action = sys.argv[1].lower()
    
    # Find USB port
    port = find_usb_port()
    if not port:
        print('{"success": false, "message": "Failed to find USB port"}')
        sys.exit(1)
    
    logger.info(f"Using port: {port}")
    
    if action == "get":
        result = get_current_mode(port)
        import json
        print(json.dumps(result))
    
    elif action == "set":
        if len(sys.argv) < 3:
            print('{"success": false, "message": "Mode parameter required"}')
            sys.exit(1)
        
        try:
            mode = int(sys.argv[2])
        except ValueError:
            print('{"success": false, "message": "Invalid mode number"}')
            sys.exit(1)
        
        result = set_network_mode(port, mode)
        import json
        print(json.dumps(result))
    
    else:
        print('{"success": false, "message": "Invalid action. Use: get or set"}')
        sys.exit(1)

if __name__ == "__main__":
    main()
