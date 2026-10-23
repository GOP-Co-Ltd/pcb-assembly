# send timeout=60.0
G28 X Y Z
M400
# send timeout=60.0
G1 Z12.5 F6000.0
M400
# send timeout=60.0
G1 X8.0 F6000.0
M400
# send timeout=60.0
G1 Y6.0 F6000.0
M400
# send timeout=30.0
G90
G1 Z0.0 F1200.0
G1 X150.0 Y10.0 F1200.0
G1 Z-2.0 F1200.0
M400
M84
