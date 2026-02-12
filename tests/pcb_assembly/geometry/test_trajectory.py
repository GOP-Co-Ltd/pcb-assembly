import pytest

from pcb_assembly.geometry.trajectory import Move, Trajectory, Waypoint, sort_by_nearest
from pcb_assembly.geometry.transform import Point2d, Point3d


class TestSortByNearest:
    """sort_by_nearest関数のテスト."""

    @pytest.mark.parametrize(
        ("positions", "start", "expected"),
        [
            ([], Point3d(0.0, 0.0, 0.0), []),
            (
                [Point3d(1.0, 1.0, 0.0)],
                Point3d(0.0, 0.0, 0.0),
                [Point3d(1.0, 1.0, 0.0)],
            ),
            (
                [
                    Point3d(1.0, 0.0, 0.0),
                    Point3d(2.0, 0.0, 0.0),
                    Point3d(3.0, 0.0, 0.0),
                ],
                Point3d(0.0, 0.0, 0.0),
                [
                    Point3d(1.0, 0.0, 0.0),
                    Point3d(2.0, 0.0, 0.0),
                    Point3d(3.0, 0.0, 0.0),
                ],
            ),
            (
                [
                    Point3d(3.0, 0.0, 0.0),
                    Point3d(2.0, 0.0, 0.0),
                    Point3d(1.0, 0.0, 0.0),
                ],
                Point3d(0.0, 0.0, 0.0),
                [
                    Point3d(1.0, 0.0, 0.0),
                    Point3d(2.0, 0.0, 0.0),
                    Point3d(3.0, 0.0, 0.0),
                ],
            ),
        ],
    )
    def test_sort_by_nearest(self, positions, start, expected):
        result = sort_by_nearest(positions, start)

        assert result == expected

    def test_greedy_behavior(self):
        # greedy nearest neighborの動作確認
        # start=(0,0) から A=(1,0), B=(0,2), C=(1,2) を巡回
        positions = [
            Point3d(0.0, 2.0, 0.0),  # B
            Point3d(1.0, 2.0, 0.0),  # C
            Point3d(1.0, 0.0, 0.0),  # A
        ]

        result = sort_by_nearest(positions, Point3d(0.0, 0.0, 0.0))

        # start(0,0)から最も近いのはA(1,0)
        assert result[0] == Point3d(1.0, 0.0, 0.0)
        # A(1,0)から最も近いのはC(1,2)
        assert result[1] == Point3d(1.0, 2.0, 0.0)
        # 残りはB(0,2)
        assert result[2] == Point3d(0.0, 2.0, 0.0)


class TestMove:
    """Moveクラスのテスト."""

    def test_default_values(self):
        move = Move()

        assert move.x is None
        assert move.y is None
        assert move.z is None
        assert move.v is None
        assert move.relative is False

    @pytest.mark.parametrize(
        ("move", "expected"),
        [
            (Move(), True),
            (Move(x=1.0), False),
            (Move(y=1.0), False),
            (Move(z=1.0), False),
            (Move(v=1.0), False),
            (Move(x=1.0, y=2.0, z=3.0), False),
        ],
    )
    def test_is_empty(self, move, expected):
        assert move.is_empty == expected

    def test_from_point(self):
        point = Point3d(1.0, 2.0, 3.0)

        move = Move.from_point(point, v=10.0, relative=True)

        assert move == Move(x=1.0, y=2.0, z=3.0, v=10.0, relative=True)

    def test_from_point_defaults(self):
        point = Point3d(1.0, 2.0, 3.0)

        move = Move.from_point(point)

        assert move == Move(x=1.0, y=2.0, z=3.0, v=None, relative=False)

    def test_from_point2d(self):
        point = Point2d(1.0, 2.0)

        move = Move.from_point(point)

        assert move == Move(x=1.0, y=2.0, z=None, v=None, relative=False)


class TestWaypoint:
    """Waypointクラスのテスト."""

    def test_position(self):
        wp = Waypoint(x=1.0, y=2.0, z=3.0, v=10.0)

        assert wp.position == Point3d(1.0, 2.0, 3.0)


class TestTrajectory:
    """Trajectoryクラスのテスト."""

    def test_init(self):
        origin = Point3d(0.0, 0.0, 0.0)
        traj = Trajectory(origin=origin, initial_velocity=10.0)

        assert traj.position == origin
        assert traj.velocity == 10.0
        assert traj.waypoints == []

    def test_init_with_moves(self):
        origin = Point3d(0.0, 0.0, 0.0)
        moves = [Move(x=1.0, y=2.0, z=3.0), Move(x=4.0, y=5.0, z=6.0)]

        traj = Trajectory(moves, origin=origin, initial_velocity=10.0)

        assert len(traj.waypoints) == 2
        assert traj.position == Point3d(4.0, 5.0, 6.0)

    def test_init_with_single_move(self):
        traj = Trajectory(
            Move(x=5.0, y=5.0, z=0.0),
            origin=Point3d(0.0, 0.0, 0.0),
            initial_velocity=10.0,
        )

        assert len(traj.waypoints) == 1
        assert traj.position == Point3d(5.0, 5.0, 0.0)

    def test_init_without_initial_velocity(self):
        traj = Trajectory(
            Move(x=5.0, y=5.0, z=0.0, v=20.0),
            origin=Point3d(0.0, 0.0, 0.0),
        )

        assert traj.velocity == 20.0

    def test_velocity_raises_when_none(self):
        traj = Trajectory(origin=Point3d(0.0, 0.0, 0.0))

        with pytest.raises(ValueError, match="velocity"):
            _ = traj.velocity

    def test_add_sets_velocity_from_first_move(self):
        traj = Trajectory(origin=Point3d(0.0, 0.0, 0.0))

        traj.add(Move(x=1.0, v=15.0))

        assert traj.velocity == 15.0

    def test_add_absolute_move(self):
        traj = Trajectory(origin=Point3d(0.0, 0.0, 0.0), initial_velocity=10.0)

        traj.add(Move(x=5.0, y=5.0, z=0.0))

        assert len(traj.waypoints) == 1
        assert traj.waypoints[0] == Waypoint(x=5.0, y=5.0, z=0.0, v=10.0)
        assert traj.position == Point3d(5.0, 5.0, 0.0)

    def test_add_relative_move(self):
        traj = Trajectory(origin=Point3d(10.0, 10.0, 0.0), initial_velocity=10.0)

        traj.add(Move(x=5.0, y=-3.0, relative=True))

        assert traj.waypoints[0] == Waypoint(x=15.0, y=7.0, z=0.0, v=10.0)

    def test_add_partial_absolute_move(self):
        traj = Trajectory(origin=Point3d(10.0, 20.0, 30.0), initial_velocity=10.0)

        traj.add(Move(x=5.0))  # y, zはNone → 現在位置を維持

        assert traj.waypoints[0] == Waypoint(x=5.0, y=20.0, z=30.0, v=10.0)

    def test_add_velocity_only(self):
        traj = Trajectory(origin=Point3d(10.0, 20.0, 30.0), initial_velocity=10.0)

        traj.add(Move(v=50.0))

        assert traj.waypoints[0] == Waypoint(x=10.0, y=20.0, z=30.0, v=50.0)
        assert traj.velocity == 50.0

    def test_add_with_velocity(self):
        traj = Trajectory(origin=Point3d(0.0, 0.0, 0.0), initial_velocity=10.0)

        traj.add(Move(x=5.0, v=20.0))

        assert traj.waypoints[0].v == 20.0
        assert traj.velocity == 20.0

    def test_add_empty_move_ignored(self):
        traj = Trajectory(origin=Point3d(0.0, 0.0, 0.0), initial_velocity=10.0)

        traj.add(Move())

        assert traj.waypoints == []

    def test_add_multiple_moves(self):
        traj = Trajectory(origin=Point3d(0.0, 0.0, 0.0), initial_velocity=10.0)

        traj.add(Move(x=1.0), Move(x=2.0), Move(x=3.0))

        assert len(traj.waypoints) == 3
        assert traj.position == Point3d(3.0, 0.0, 0.0)

    def test_add_iterable(self):
        traj = Trajectory(origin=Point3d(0.0, 0.0, 0.0), initial_velocity=10.0)
        moves = [Move(x=1.0), Move(x=2.0)]

        traj.add(move=moves)

        assert len(traj.waypoints) == 2

    @pytest.mark.parametrize(
        ("moves", "expected_distance"),
        [
            ([], 0.0),
            # origin=(0,0,0) → waypoint=(0,0,0): distance=0
            ([Move(x=0.0, y=0.0, z=0.0)], 0.0),
            # origin=(0,0,0) → (0,0,0) → (3,4,0): 0+5=5
            ([Move(x=0.0, y=0.0, z=0.0), Move(x=3.0, y=4.0, z=0.0)], 5.0),
            # origin=(0,0,0) → (0,0,0) → (3,4,0) → (3,4,5): 0+5+5=10
            (
                [
                    Move(x=0.0, y=0.0, z=0.0),
                    Move(x=3.0, y=4.0, z=0.0),
                    Move(x=3.0, y=4.0, z=5.0),
                ],
                10.0,
            ),
        ],
    )
    def test_distance(self, moves, expected_distance):
        traj = Trajectory(origin=Point3d(0.0, 0.0, 0.0), initial_velocity=10.0)
        traj.add(*moves)

        assert traj.distance() == expected_distance

    def test_distance_includes_origin_to_first_waypoint(self):
        traj = Trajectory(origin=Point3d(0.0, 0.0, 0.0), initial_velocity=10.0)
        traj.add(Move(x=3.0, y=4.0, z=0.0))

        # origin(0,0,0) → (3,4,0) = 5.0
        assert traj.distance() == 5.0

    @pytest.mark.parametrize(
        ("moves", "expected_time"),
        [
            ([], 0.0),
            ([Move(x=0.0, y=0.0, z=0.0)], 0.0),
            ([Move(x=0.0, y=0.0, z=0.0), Move(x=10.0, y=0.0, z=0.0)], 1.0),
            ([Move(x=0.0, y=0.0, z=0.0), Move(x=10.0, y=0.0, z=0.0, v=5.0)], 2.0),
            (
                [
                    Move(x=0.0, y=0.0, z=0.0),
                    Move(x=10.0, y=0.0, z=0.0),
                    Move(x=10.0, y=20.0, z=0.0, v=20.0),
                ],
                2.0,
            ),
        ],
    )
    def test_time(self, moves, expected_time):
        traj = Trajectory(origin=Point3d(0.0, 0.0, 0.0), initial_velocity=10.0)
        traj.add(*moves)

        assert traj.time() == expected_time

    def test_time_includes_origin_to_first_waypoint(self):
        traj = Trajectory(origin=Point3d(0.0, 0.0, 0.0), initial_velocity=10.0)
        traj.add(Move(x=10.0, y=0.0, z=0.0))

        # origin(0,0,0) → (10,0,0), distance=10, velocity=10, time=1.0
        assert traj.time() == 1.0
