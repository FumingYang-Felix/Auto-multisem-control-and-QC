"""
COM connection management for MultiSEM
"""

from __future__ import annotations
from typing import Optional
import comtypes.client as cc


class MSEMConnection:
    """
    Manages COM connection to MultiSEM.
    
    Usage:
        conn = MSEMConnection()
        conn.connect()
        conn.initialize()
        
        # Use conn.cp for direct API calls
        x = conn.get_double("AP_STAGE_AT_X")
        conn.set_double("AP_STAGE_GOTO_X", 1000.0)
        ret = conn.execute("CMD_WAIT_STAGE_SETTLED")
    """
    
    COM_PROG_ID = "ZeissmSEMWrapper.Commands"
    
    def __init__(self) -> None:
        self._cp = None
        self._connected = False
        self._initialized = False
    
    @property
    def cp(self):
        """Direct access to COM object for advanced usage"""
        return self._cp
    
    @property
    def is_connected(self) -> bool:
        return self._connected
    
    @property
    def is_initialized(self) -> bool:
        return self._initialized
    
    def connect(self) -> None:
        """Create COM object connection"""
        if self._connected:
            return
        self._cp = cc.CreateObject(self.COM_PROG_ID)
        self._connected = True
    
    def initialize(self) -> int:
        """
        Execute CMD_INITIALIZE.
        Returns 0 on success.
        """
        if not self._connected:
            raise RuntimeError("Not connected. Call connect() first.")
        ret = self._cp.Execute("CMD_INITIALIZE")
        if ret == 0:
            self._initialized = True
        return ret
    
    def connect_and_initialize(self) -> int:
        """Convenience method: connect + initialize in one call"""
        self.connect()
        return self.initialize()
    
    # ========== Low-level API wrappers ==========
    
    def execute(self, command: str) -> int:
        """Execute a command, returns result code"""
        return self._cp.Execute(command)
    
    def get_double(self, param: str) -> float:
        """Get a double parameter value"""
        return self._cp.Get_ReturnTypeDouble(param)
    
    def set_double(self, param: str, value: float) -> None:
        """Set a double parameter value"""
        self._cp.Set_PassedTypeDouble(param, value)
    
    def get_string(self, param: str) -> str:
        """Get a string parameter value"""
        return self._cp.Get_ReturnTypeString(param)
    
    def set_string(self, param: str, value: str) -> None:
        """Set a string parameter value"""
        self._cp.Set_PassedTypeString(param, value)
    
    def get_double_from_list(self, param: str, index: int) -> float:
        """Get a double value from a list parameter at the given index"""
        return self._cp.Get_DoubleFromList(param, index)
    
    def get_array(self, param: str) -> list:
        """Get an array parameter value"""
        return self._cp.Get_Array(param)
    
    def get_last_error(self) -> str:
        """Get last error message"""
        try:
            return self.get_string("STR_LAST_ERROR")
        except Exception:
            return ""

