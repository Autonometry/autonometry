"""Connectors: pure engines that turn a robot's own signals into recorder messages.

Each engine is standard library only and does no input or output; the process that reads the
robot (for example the ROS 2 node in ``connectors/ros2``) feeds it observations and sends what it
returns through the recorder client.
"""
