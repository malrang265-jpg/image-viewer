import sys
import os
from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt
from ui.viewer import ImageViewer

def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    
    viewer = ImageViewer()
    
    if len(sys.argv) > 1:
        viewer.load_path(sys.argv[1])
    
    viewer.show()
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()
