"""
Main Entry Point
Starts the PySide6 GUI and Workflow Engine.
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication
from msem_control.engine.workflow import WorkflowEngine
from msem_control.ui.main_window import MainWindow


def main():
    print("=== MultiSEM Control Software (GUI) ===")
    
    # 1. Initialize Engine (Simulation Mode)
    # Set simulation_mode=False to connect to real hardware on Windows
    engine = WorkflowEngine(simulation_mode=True)
    
    try:
        # 2. Start GUI
        app = QApplication(sys.argv)
        
        window = MainWindow(engine)
        window.show()
        
        # 3. Event Loop
        exit_code = app.exec()
        
    finally:
        print("Shutting down engine...")
        engine.shutdown()
        
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
