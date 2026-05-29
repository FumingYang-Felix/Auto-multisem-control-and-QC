"""
Workflow Engine
Orchestrates the entire acquisition process.
Acts as the bridge between UI and MSEM hardware.
"""
import threading
import time
import sys
import os
from typing import Callable, Optional
from dataclasses import dataclass

# Add msem to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


@dataclass
class StagePosition:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class WorkflowEngine:
    """
    Main workflow engine that bridges UI and MSEM hardware.
    
    In simulation mode: Uses mock values
    In real mode: Uses msem module to control hardware
    """
    
    def __init__(self, simulation_mode: bool = True):
        self.simulation_mode = simulation_mode
        self._connected = False
        self._running = True
        # When True, the engine must NOT touch the hardware: the acquisition
        # subprocess (focus_map via main_live_qc) owns the scope during a run.
        # The Zeiss API warns that two software components driving the same
        # hardware don't know about each other, so we hard-stop the poll loop
        # + manual ops while a run is in progress.
        self._paused = False

        # Hardware interfaces (None until connected)
        self._conn = None
        self._stage = None
        self._acq = None
        self._autofocus = None
        self._autostig = None
        
        # Current state
        self._current_position = StagePosition()
        self._current_operation = "idle"
        self._beam_on = False
        
        # Callbacks for UI
        self.on_log: Optional[Callable[[str, str], None]] = None
        self.on_status: Optional[Callable[[str], None]] = None
        self.on_position: Optional[Callable[[float, float, float], None]] = None
        self.on_connected: Optional[Callable[[bool], None]] = None
        self.on_operation: Optional[Callable[[str, int], None]] = None
        self.on_beam_status: Optional[Callable[[bool], None]] = None
        self.on_all_status: Optional[Callable[[dict], None]] = None  # For comprehensive status updates
        
        # Background thread for position updates
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def _log(self, msg: str, level: str = "INFO"):
        if self.on_log:
            self.on_log(msg, level)
        print(f"[{level}] {msg}")

    def _set_operation(self, op: str, progress: int = -1):
        self._current_operation = op
        if self.on_operation:
            self.on_operation(op, progress)
            
    # ========== Connection ==========

    def connect(self):
        """Connect to MSEM hardware"""
        self._set_operation("connecting")
        
        if self.simulation_mode:
            # Simulate connection
            self._log("Simulation mode: Connecting...")
            time.sleep(0.5)
            self._connected = True
            self._log("Simulation mode: Connected successfully")
        else:
            try:
                # Import and use real msem module
                from msem.connection import MSEMConnection
                from msem.stage import Stage
                from msem.acquisition import Acquisition
                from msem.autofocus import Autofocus
                from msem.autostig import Autostig
                
                self._log("Connecting to MultiSEM...")
                self._conn = MSEMConnection()
                self._conn.connect()
                
                # Initialize
                ret = self._conn.execute("CMD_INITIALIZE")
                if ret != 0:
                    self._log(f"Initialize returned: {ret}", "WARNING")
                    
                # Create module instances
                self._stage = Stage(self._conn)
                self._acq = Acquisition(self._conn)
                self._autofocus = Autofocus(self._conn)
                self._autostig = Autostig(self._conn)
                
                self._connected = True
                self._log("Connected to MultiSEM successfully")
                
                # Get initial position
                self._update_position()
                
            except Exception as e:
                self._log(f"Connection failed: {e}", "ERROR")
                self._connected = False
                
        if self.on_connected:
            self.on_connected(self._connected)
            
        self._set_operation("idle")
        return self._connected

    def disconnect(self):
        """Disconnect from MSEM"""
        self._connected = False
        self._conn = None
        self._stage = None
        self._acq = None
        self._autofocus = None
        self._autostig = None
        
        self._log("Disconnected from MultiSEM")
        
        if self.on_connected:
            self.on_connected(False)

    def is_connected(self) -> bool:
        return self._connected
    
    def get_msem_connection(self):
        """Get the underlying MSEM connection object for advanced operations"""
        return self._conn

    # ========== Hardware ownership (acquisition coexistence) ==========

    def pause_hardware(self):
        """Stop the engine touching the scope. Call before launching the
        acquisition subprocess so the GUI's poll loop + manual ops don't
        collide with the subprocess driving the hardware."""
        self._paused = True
        self._log("Hardware handed to acquisition subprocess (engine polling paused)")

    def resume_hardware(self):
        """Re-enable engine polling + manual ops after the subprocess finishes."""
        self._paused = False
        self._log("Hardware released back to engine (polling resumed)")

    def is_paused(self) -> bool:
        return self._paused

    # ========== Stage Control ==========

    def move_stage(self, x: float, y: float, z: float = None):
        """Move stage to position. Blocks until complete."""
        if not self._connected:
            self._log("Not connected", "ERROR")
            return
        if self._paused:
            self._log("Acquisition in progress — manual stage move ignored", "WARNING")
            return

        self._set_operation("moving")
        self._log(f"Moving stage to X={x:.1f}, Y={y:.1f}")
        
        def do_move():
            if self.simulation_mode:
                # Simulate movement
                steps = 20
                start_x, start_y = self._current_position.x, self._current_position.y
                for i in range(steps):
                    t = (i + 1) / steps
                    self._current_position.x = start_x + (x - start_x) * t
                    self._current_position.y = start_y + (y - start_y) * t
                    if self.on_position:
                        self.on_position(
                            self._current_position.x, 
                            self._current_position.y, 
                            self._current_position.z
                        )
                    time.sleep(0.05)
            else:
                # Real hardware - move with wait=False and poll position
                if z is not None:
                    self._stage.move_xyz(x, y, z, wait=False)
                else:
                    self._stage.move_xy(x, y, wait=False)
                
                # Poll position every 0.25s until we're close to target (within 1 µm)
                max_wait_time = 60.0  # Maximum 60 seconds
                start_time = time.time()
                
                while time.time() - start_time < max_wait_time:
                    self._update_position()
                    if self.on_position:
                        self.on_position(
                            self._current_position.x,
                            self._current_position.y,
                            self._current_position.z
                        )
                    
                    # Check if we're close enough to target
                    dx = abs(self._current_position.x - x)
                    dy = abs(self._current_position.y - y)
                    if dx < 1.0 and dy < 1.0:  # Within 1 µm
                        break
                    
                    time.sleep(0.25)  # Poll every 250ms
                
                # Final position update
                self._update_position()
                if self.on_position:
                    self.on_position(
                        self._current_position.x,
                        self._current_position.y,
                        self._current_position.z
                    )
                
            self._log(f"Stage move complete")
            self._set_operation("idle")
            
        # Run in thread to not block UI
        thread = threading.Thread(target=do_move)
        thread.start()

    def get_position(self) -> StagePosition:
        """Get current stage position"""
        if self._connected and not self.simulation_mode and self._stage:
            self._update_position()
        return self._current_position

    def _update_position(self):
        """Update position from hardware"""
        if self._stage:
            try:
                pos = self._stage.get_position()
                self._current_position.x = pos.x
                self._current_position.y = pos.y
                self._current_position.z = pos.z if pos.z else 0
                
                if self.on_position:
                    self.on_position(pos.x, pos.y, pos.z if pos.z else 0)
            except Exception as e:
                self._log(f"Failed to get position: {e}", "ERROR")

    # ========== Acquisition ==========

    def acquire_single(self):
        """Acquire a single image"""
        if not self._connected:
            self._log("Not connected", "ERROR")
            return
        if self._paused:
            self._log("Acquisition in progress — manual acquire ignored", "WARNING")
            return

        self._set_operation("acquiring")
        
        def do_acquire():
            if self.simulation_mode:
                self._log("Simulation: Acquiring image...")
                time.sleep(2.0)
                self._log("Simulation: Image acquired")
            else:
                ret = self._acq.acquire_single()
                if ret == 0:
                    self._log("Image acquired successfully")
                else:
                    self._log(f"Acquisition failed with code: {ret}", "ERROR")
                    
            self._set_operation("idle")
            
        thread = threading.Thread(target=do_acquire)
        thread.start()

    # ========== Autofocus ==========

    def run_autofocus(self):
        """Run autofocus"""
        if not self._connected:
            self._log("Not connected", "ERROR")
            return
        if self._paused:
            self._log("Acquisition in progress — manual autofocus ignored", "WARNING")
            return

        self._set_operation("autofocusing")
        
        def do_af():
            if self.simulation_mode:
                self._log("Simulation: Running autofocus...")
                time.sleep(3.0)
                self._log("Simulation: Autofocus complete")
            else:
                result = self._autofocus.run()
                if result.success:
                    self._log(f"Autofocus complete. Confidence: {result.confidence:.2f}")
                else:
                    self._log(f"Autofocus failed: {result.message}", "ERROR")
                    
            self._set_operation("idle")
            
        thread = threading.Thread(target=do_af)
        thread.start()

    # ========== Autostig ==========

    def run_autostig(self):
        """Run autostigmation"""
        if not self._connected:
            self._log("Not connected", "ERROR")
            return
        if self._paused:
            self._log("Acquisition in progress — manual autostig ignored", "WARNING")
            return

        self._set_operation("autostig")
        
        def do_stig():
            if self.simulation_mode:
                self._log("Simulation: Running autostig...")
                time.sleep(2.5)
                self._log("Simulation: Autostig complete")
            else:
                result = self._autostig.run()
                if result.success:
                    self._log(f"Autostig complete. Confidence: X={result.confidence_x:.2f}, Y={result.confidence_y:.2f}")
                else:
                    self._log(f"Autostig failed: {result.message}", "ERROR")
                    
            self._set_operation("idle")
            
        thread = threading.Thread(target=do_stig)
        thread.start()

    # ========== Background Polling ==========

    def _poll_loop(self):
        """Background loop to poll status from hardware"""
        self._last_status_cache = {}
        
        while self._running:
            if self._connected and not self._paused:
                if self.simulation_mode:
                    # Simulation mode - emit mock status
                    try:
                        self._emit_simulation_status()
                    except Exception as e:
                        self._log(f"Simulation status error: {e}", "WARNING")
                else:
                    # Real hardware - poll all status
                    if self._current_operation == "idle":
                        try:
                            self._poll_all_status()
                        except Exception as e:
                            self._log(f"Status poll error: {e}", "WARNING")
            time.sleep(1.0)  # Poll every 1 second instead of 0.5
            
    def _emit_simulation_status(self):
        """Emit simulated status for simulation mode"""
        status = {
            'DP_IS_HARDWARE_CONNECTED': 0.0,  # Simulation mode
            'AP_STAGE_AT_X': self._current_position.x,
            'AP_STAGE_AT_Y': self._current_position.y,
            'AP_STAGE_AT_Z': self._current_position.z,
            'DP_IS_STAGE_MOVING': 1.0 if self._current_operation == "moving" else 0.0,
            'DP_IS_BEAM_ON': 1.0,
            'DP_IS_BEAMLOCK_ON': 1.0,
            'AP_EMITTER_CURRENT': 1.5,
            'DP_VACSTATUS': 2.0,  # Normal vacuum
            'AP_PRESSURE_CHAMBER': 1e-6,
            'AP_PRESSURE_GUN': 1e-8,
            'AP_PRESSURE_UPPERCOLUMN': 1e-7,
            'AP_PRESSURE_MICROOPTICS': 1e-7,
            'AP_PRESSURE_SCINTILLATOR': 1e-6,
            'DP_IS_ECHUCK_ON': 1.0,
            'AP_ECHUCK_VOLTAGE_MONITOR': 1000.0,
            'AP_ECHUCK_CURRENT_MONITOR': 0.5,
        }
        
        # Only emit if position has changed
        if self._has_status_changed(status):
            self._last_status_cache = status.copy()
            
            if self.on_all_status:
                self.on_all_status(status)
                
            if self.on_position:
                self.on_position(
                    status['AP_STAGE_AT_X'],
                    status['AP_STAGE_AT_Y'],
                    status['AP_STAGE_AT_Z']
                )
    
    def _poll_all_status(self):
        """Poll all status parameters from hardware"""
        if not self._conn:
            return
            
        try:
            status = {}
            
            # Hardware connection
            status['DP_IS_HARDWARE_CONNECTED'] = self._conn.get_double('DP_IS_HARDWARE_CONNECTED')
            
            # Stage position
            status['AP_STAGE_AT_X'] = self._conn.get_double('AP_STAGE_AT_X')
            status['AP_STAGE_AT_Y'] = self._conn.get_double('AP_STAGE_AT_Y')
            status['AP_STAGE_AT_Z'] = self._conn.get_double('AP_STAGE_AT_Z')
            status['DP_IS_STAGE_MOVING'] = self._conn.get_double('DP_IS_STAGE_MOVING')
            
            # Update internal position
            self._current_position.x = status['AP_STAGE_AT_X']
            self._current_position.y = status['AP_STAGE_AT_Y']
            self._current_position.z = status['AP_STAGE_AT_Z']
            
            # Beam status
            status['DP_IS_BEAM_ON'] = self._conn.get_double('DP_IS_BEAM_ON')
            status['DP_IS_BEAMLOCK_ON'] = self._conn.get_double('DP_IS_BEAMLOCK_ON')
            status['AP_EMITTER_CURRENT'] = self._conn.get_double('AP_EMITTER_CURRENT')
            
            # Vacuum
            status['DP_VACSTATUS'] = self._conn.get_double('DP_VACSTATUS')
            status['AP_PRESSURE_CHAMBER'] = self._conn.get_double('AP_PRESSURE_CHAMBER')
            status['AP_PRESSURE_GUN'] = self._conn.get_double('AP_PRESSURE_GUN')
            status['AP_PRESSURE_UPPERCOLUMN'] = self._conn.get_double('AP_PRESSURE_UPPERCOLUMN')
            status['AP_PRESSURE_MICROOPTICS'] = self._conn.get_double('AP_PRESSURE_MICROOPTICS')
            status['AP_PRESSURE_SCINTILLATOR'] = self._conn.get_double('AP_PRESSURE_SCINTILLATOR')
            
            # E-Chuck
            status['DP_IS_ECHUCK_ON'] = self._conn.get_double('DP_IS_ECHUCK_ON')
            status['AP_ECHUCK_VOLTAGE_MONITOR'] = self._conn.get_double('AP_ECHUCK_VOLTAGE_MONITOR')
            status['AP_ECHUCK_CURRENT_MONITOR'] = self._conn.get_double('AP_ECHUCK_CURRENT_MONITOR')
            
            # Only emit if status has changed (compare with cache)
            if self._has_status_changed(status):
                self._last_status_cache = status.copy()
                
                if self.on_all_status:
                    self.on_all_status(status)
                    
                if self.on_position:
                    self.on_position(
                        status['AP_STAGE_AT_X'],
                        status['AP_STAGE_AT_Y'],
                        status['AP_STAGE_AT_Z']
                    )
                
        except Exception as e:
            self._log(f"Error polling status: {e}", "ERROR")
            
    def _has_status_changed(self, new_status: dict) -> bool:
        """Check if status has changed significantly"""
        if not hasattr(self, '_last_status_cache') or not self._last_status_cache:
            return True
            
        for key, value in new_status.items():
            if key not in self._last_status_cache:
                return True
            
            old_value = self._last_status_cache[key]
            
            # For position values, only update if changed by more than 0.1 µm
            if key.startswith('AP_STAGE_AT_'):
                if abs(value - old_value) > 0.1:
                    return True
            # For pressure values, only update if changed by more than 1%
            elif key.startswith('AP_PRESSURE_'):
                if old_value > 0 and abs(value - old_value) / old_value > 0.01:
                    return True
            # For boolean/status values, any change triggers update
            else:
                if value != old_value:
                    return True
                    
        return False

    def shutdown(self):
        """Clean shutdown"""
        self._running = False
        if self._connected:
            self.disconnect()
