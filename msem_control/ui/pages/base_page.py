from PySide6.QtWidgets import QWidget, QVBoxLayout

class BasePage(QWidget):
    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(20, 20, 20, 20)
        self.setup_ui()

    def setup_ui(self):
        """Override to build UI"""
        pass
        
    def on_show(self):
        """Called when page becomes visible"""
        pass

