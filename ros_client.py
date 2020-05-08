import roslibpy as rlp
import roslibpy.actionlib
import numpy as np
import math
import quaternion # numpy-quaternion
from collections import deque
import time
from threading import Timer

# import matplotlib.pyplot as plt

roslibpy.actionlib.DEFAULT_CONNECTION_TIMEOUT = 10

class RosClient():
    def __init__(self, master_name :str, port :int):
        self.master_name = master_name
        self.port = port
        self.client = rlp.Ros(self.master_name, port=self.port) #rlp.Ros('10.244.1.117', port=9090)\
        self.client.on_ready(lambda: print('is ROS connected: ', self.client.is_connected))
        self.client.run()

        self.services = {} # {service_name: rlp.Service}
        #botsu# {service_name: {'service': rlp.Service, 'args': {args_key: []}}}

    def register_servise(self, service_name: str, service_type: str):
        self.services[service_name] = rlp.Service(self.client, service_name, service_type)

    def call_service(self, service_name: str, args: list=None):
        srv = self.services[service_name]
        req = rlp.ServiceRequest(args)
        return srv.call(req)

## FCFS (First Come First Served）
class ActionScheduler(Timer):
    STATUS_READY_TO_START = 0b01
    def __init__(self, ros_client: rlp.Ros, server_name: str, action_name: str, callback: callable, queue_size: int=100, rate: float=0.05, args: list=None, kwargs: dict=None): 
        Timer.__init__(self, rate, self.run, args, kwargs)
        self.ros_client = ros_client
        self.action_client = rlp.actionlib.ActionClient(ros_client, server_name, action_name)
        self.current_goal: rlp.actionlib.Goal = None
        self.callback = callback
        self.thread = None
        self.goal_queue = deque(maxlen=queue_size)
        self.rate = rate
        self.state = ActionScheduler.STATUS_READY_TO_START ## 01: queue is empty, 10: task is actived
        
    def _update_state(self):
        is_emp = 1 if len(self.goal_queue) == 0 else 0
        is_fin = 0
        if self.current_goal is not None:
            is_fin = 0 if self.current_goal.is_finished else 1
        self.state = is_emp|(is_fin<<1)

    def _check_task(self):
        self._update_state()
        self.thread = Timer(self.rate, self._check_task)
        self.thread.start()

        if not self.state&1 and not self.state>>1&1:
            message_closure = self.goal_queue.popleft()
            self.current_goal = rlp.actionlib.Goal(self.action_client, message_closure())
            self._update_state()
            self.current_goal.send(self._finish)

    def _finish(self, result):
        self._update_state()
        self.callback(result)

    ## need to change data type of goal to such as  <Goal, tag>, to be able to cancel
    def append_goal(self, message_closure):
        self.goal_queue.append(message_closure)
        self._update_state()

    def run(self):
        self._check_task()

    def cancel(self):
        if self.thread is not None:
            self.thread.cancel()
            self.thread.join()
            del self.thread

## make message that has subscribed synchronized in time approximate, before call a callback function
class TimeSynchronizer():
    class Subscriber():
        def __init(self, sync, listener: rlp.Topic, queue_size: int):
            self.sync = sync
            self.listener = listener
            self.queue = deque(maxlen=queue_size)
            self.listener.subscribe(self.cb)
        
        def cb(self, message):
            self.queue.append(message)
            self.sync.synchronize()

    def __init__(self, topics: list, callback: callable, queue_size: int, allow_headerless: float=None):
        self.listeners = [TimeSynchronizer.Subscriber(self, s, queue_size) for s in topics]
        self.callback = callback
        self.allow_headerless = allow_headerless
        self.queue = deque(maxlen=queue_size)

    def get_time(self, message):
        return message['header']['stamp']['secs']*(10**9)+message['header']['stamp']['nsecs']

    def synchronize(self):
        if not all([len(s.queue) for s in self.listeners]):
            return

        lis_len = len(self.listeners)
        di = {i:t for i,t in zip(range(lis_len), map(self.get_time, [l.queue[0] for l in self.listeners]))}
        cri_time_i = max(di, key=di.get)
        cri_time = di[cri_time_i]
        allow_time = cri_time + self.allow_headerless
        del di[cri_time_i]

        result = [None for x in range(lis_len)]
        result[cri_time_i] = self.listeners[cri_time_i].queue[0]

        for k in di.keys():
            # al = np.array([i for i in map(self.get_time, self.listeners[k].queue)])
            tq = deque()
            for i in range(len(self.listeners[k].queue)):
                nt = self.get_time(self.listeners[k].queue[i])
                if nt <= allow_time:
                    tq.append(nt)
                else:
                    break
            
            arr = np.abs(np.array(tq)-cri_time)
            abs_min_i = np.argmin(arr)
            result[k] = self.listeners[k].queue[abs_min_i]
            for i in range(abs_min_i+1):
                self.listeners[k].queue.popleft()
        
        self.callback(*result)

## class that create and send goal of pose to move_base
class MobileClient():
    def __init__(self, ros_client: rlp.Ros, goal_callback: callable, odom_topic: str='/odom', map_topic: str='/map'):
        self.ros_client = ros_client
        self.result_callback = goal_callback
        self.mb_scheduler = ActionScheduler(self.ros_client, '/move_base', 'move_base_msgs/MoveBaseAction', self.result_callback)

        self.is_get_map = False
        self.map_listener = rlp.Topic(self.ros_client, map_topic, 'nav_msgs/OccupancyGrid')
        self.map_listener.subscribe(self._update_map)
        
        self.is_get_odom = False
        self.odom_listener = rlp.Topic(self.ros_client, odom_topic, 'nav_msgs/Odometry')
        self.odom_listener.subscribe(self._update_odometry)

    @property
    def is_reached(self):
        return not self.mb_scheduler.state>>1

    ## need to make to be changed design pattern 'update_**'
    def _update_map(self, message):
        self.map_header = message['header']
        self.map_info = message['info']
        self.map_padsize_x = (self.map_info['width']-1)//2
        self.map_padsize_y = (self.map_info['height']-1)//2
        self.map = np.array(message['data']).reshape([self.map_info['height'],self.map_info['width']])
        self.is_get_map = True

    def _update_odometry(self, message):
        pos = message['pose']['pose']['position']
        ori = message['pose']['pose']['orientation']
        self.position = np.array([pos['x'], pos['y'], pos['z']])
        self.orientation = np.quaternion(ori['w'], ori['x'], ori['y'], ori['z'])
        self.is_get_odom = True

    def wait_for_ready(self, timeout=10.0):
        print('wait for ros message...', end=' ')
        sec = 0.001
        end_time = time.time() + timeout
        while True:
            if self.is_get_odom and self.is_get_map:
                print('got ready')
                break
            elif time.time()+sec >= end_time:
                self.ros_client.terminate()
                raise Exception('Timeout you can\'t get map or odometry.')
            time.sleep(sec)

    @staticmethod
    def get_relarive_orientation(rel_vec: np.quaternion) -> np.quaternion:
        ## rotate angle (alpha,beta,gamma):(atan(z/y),atan(x/z),atan(x/y))
        to_angle = (math.atan2(rel_vec.z, rel_vec.y), math.atan2(rel_vec.x, rel_vec.z), math.atan2(rel_vec.x,rel_vec.y))
        return quaternion.from_euler_angles(to_angle)

    @staticmethod
    def get_base_pose(base_vec: np.quaternion, base_orient: np.quaternion, rel_vec: np.quaternion, rel_orient: np.quaternion) -> (np.quaternion, np.quaternion):
        t = (-base_orient) * rel_vec * (-base_orient).conj()
        goal_vec = base_vec+t
        goal_orient = base_orient*rel_orient
        return goal_vec, goal_orient

    def get_base_pose_from_body(self, position: np.quaternion, orientation=np.quaternion(1,0,0,0)):
        body_pos = np.quaternion(0, *self.position)
        return self.get_base_pose(body_pos, self.orientation, position, orientation)

    ## map img's (i,j) to base map's (x,y)
    def get_coordinates_from_map(self, ij: tuple) -> tuple:
        return ij[1] - self.map_padsize_x, ij[0] - self.map_padsize_y

    ## base map's (x,y) to base map's (i,j)
    def get_index_from_coordinates(self, xy: tuple) -> tuple:
        return xy[1] + self.map_padsize_y, xy[0] - self.map_padsize_x

    def create_message_move_base_goal(self, position: tuple, orientation: np.quaternion) -> rlp.Message:
        message = {
            'target_pose': {
                'header': self.map_header,
                'pose': {
                    'position': {
                        'x': position[0],
                        'y': position[1],
                        'z': position[2]
                    },
                    'orientation': {
                        'x': orientation.x,
                        'y': orientation.y,
                        'z': orientation.z,
                        'w': orientation.w
                    }
                }
            }
        }
        return rlp.Message(message)

    ## set goal message that simple ahead pose
    def set_goal_relative_xy(self, x, y, angle=None, is_dynamic=False):
        rel_pos_2d = np.quaternion(0,x,y,0)
        rel_ori = quaternion.from_euler_angles(0, 0, math.atan2(x,y))
        pos, ori = self.get_base_pose_from_body(rel_pos_2d, rel_ori*self.orientation)

        def inner():
            if is_dynamic:
                dpos, dori = self.get_base_pose_from_body(rel_pos_2d, rel_ori*self.orientation)
                return self.create_message_move_base_goal((dpos.x, dpos.y, dpos.z), dori)
            else:
                return self.create_message_move_base_goal((pos.x, pos.y, pos.z), ori)

        self.mb_scheduler.append_goal(inner)
        print(f'scheduling ({pos.x},{pos.y}, {pos.z})')

    def start(self):
        self.mb_scheduler.run()

    def stop(self):
        self.mb_scheduler.cancel()

def main():
    # rc = RosClient('10.244.1.117', 9090)
    rc = RosClient('10.244.1.176', 9090)
    # rc = RosClient('localhost', 9090)
    # rc.register_servise('/dynamic_map', 'nav_msgs/GetMap')
    # print(rc.call_service('/dynamic_map'))

    # odomname = '/odometry/filtered'
    odomname = '/odom'
    ms = MobileClient(rc.client, lambda r: print('reached goal', r), odom_topic=odomname)
    ms.wait_for_ready(timeout=80)
    print('map_header: ', ms.map_header)
    print('odom:', ms.position, [math.degrees(v) for v in quaternion.as_euler_angles(ms.orientation)])

    ## you can set goal any time not only after call start().
    ms.start() ## make goal appended to queue, executable
    ms.set_goal_relative_xy(0, 0.5, True) ## set scheduler a goal that go ahead 2 from robot body
    # ms.set_goal_relative(3, 3) ## relative (x:right:3, y:front:3)
    time.sleep(30)
    ms.set_goal_relative_xy(0,0.5, True)
    time.sleep(30)
    ms.stop()
    print('finish')

    rc.client.terminate()

if __name__ == '__main__':
    main()