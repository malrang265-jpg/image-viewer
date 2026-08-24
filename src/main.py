import sys
import os

# 현재 디렉토리를 Python 경로에 추가
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
