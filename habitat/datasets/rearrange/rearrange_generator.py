#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import os.path as osp
from typing import Any, Dict, List, Optional, Set, Tuple

import magnum as mn
import numpy as np
from yacs.config import CfgNode as CN

import habitat.datasets.rearrange.samplers as samplers
import habitat.datasets.rearrange.sim_utilities as sutils
import habitat_sim
from habitat.datasets.rearrange.rearrange_dataset import RearrangeEpisode


class RearrangeEpisodeGenerator:
    def __enter__(self) -> "RearrangeEpisodeGenerator":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.sim != None:
            self.sim.close(destroy=True)
            del self.sim

    def __init__(self, cfg: CN, debug_visualization: bool = False) -> None:
        """
        Initialize the generator object for a particular configuration.
        Loads yaml, sets up samplers and debug visualization settings.
        """
        # load and cache the config
        self.cfg = cfg
        self.start_cfg = self.cfg.clone()

        # debug visualization settings
        self._render_debug_obs = self._make_debug_video = debug_visualization
        self.vdb: sutils.DebugVisualizer = (
            None  # visual debugger initialized with sim
        )

        # hold a habitat Simulator object for efficient re-use
        self.sim: habitat_sim.Simulator = None
        # initialize an empty scene and load the SceneDataset
        self.initialize_sim("NONE", self.cfg.dataset_path)

        # Setup the sampler caches from config
        self._get_resource_sets()
        self._get_scene_sampler()
        self._get_obj_samplers()
        self._get_ao_state_samplers()

        # cache objects sampled by this generator for the most recent episode
        self.ep_sampled_objects: List[
            habitat_sim.physics.ManagedRigidObject
        ] = []
        self.num_ep_generated = 0

    def _get_resource_sets(self) -> None:
        """
        Extracts scene, object, and receptacle sets from the yaml config file and constructs dicts for later reference.
        """
        self._scene_sets: Dict[str, List[str]] = {}
        self._obj_sets: Dict[str, List[str]] = {}
        self._receptacle_sets: Dict[str, List[str]] = {}

        for scene_set_name, scenes in self.cfg.scene_sets:
            self._scene_sets[scene_set_name] = scenes

        for obj_set_name, objects in self.cfg.object_sets:
            self._obj_sets[obj_set_name] = objects

        for receptacle_set_name, receptacles in self.cfg.receptacle_sets:
            self._receptacle_sets[receptacle_set_name] = receptacles

        print(f"self._scene_sets = {self._scene_sets}")
        print(f"self._obj_sets = {self._obj_sets}")
        print(f"self._receptacle_sets = {self._receptacle_sets}")

    def _get_obj_samplers(self) -> None:
        """
        Extracts object sampler parameters from the yaml config file and constructs the sampler objects.
        """
        self._obj_samplers: Dict[str, samplers.ObjectSampler] = {}

        for (
            obj_sampler_name,
            obj_sampler_type,
            params,
        ) in self.cfg.obj_samplers:
            assert (
                obj_sampler_name not in self._obj_samplers
            ), f"Duplicate object sampler name '{obj_sampler_name}' in config."
            if obj_sampler_type == "uniform":
                # merge and flatten object and receptacle sets
                objects = [x for y in params[0] for x in self._obj_sets[y]]
                receptacles = [
                    x for y in params[1] for x in self._receptacle_sets[y]
                ]
                print(f"objects = {objects}")
                print(f"receptacles = {receptacles}")
                self._obj_samplers[obj_sampler_name] = samplers.ObjectSampler(
                    objects, receptacles, (params[2], params[3]), params[4]
                )
            else:
                print(
                    f"Requested object sampler '{obj_sampler_type}' is not implemented."
                )
                raise (NotImplementedError)

    def _get_object_target_samplers(self) -> None:
        """
        Initialize target samplers. Expects self.episode_data to be populated by object samples.
        """
        self._target_samplers: Dict[str, samplers.ObjectTargetSampler] = {}
        for (
            target_sampler_name,
            target_sampler_type,
            params,
        ) in self.cfg.obj_target_samplers:
            assert (
                target_sampler_name not in self._target_samplers
            ), f"Duplicate target sampler name '{target_sampler_name}' in config."
            if target_sampler_type == "uniform":
                # merge and flatten object and receptacle sets
                object_instances = [
                    x
                    for y in params[0]
                    for x in self.episode_data["sampled_objects"][y]
                ]
                receptacles = [
                    x for y in params[1] for x in self._receptacle_sets[y]
                ]
                print(f"object instances = {object_instances}")
                print(f"receptacles = {receptacles}")
                self._target_samplers[
                    target_sampler_name
                ] = samplers.ObjectTargetSampler(
                    object_instances,
                    receptacles,
                    (params[2], params[3]),
                    params[4],
                )
            else:
                print(
                    f"Requested target sampler '{target_sampler_type}' is not implemented."
                )
                raise (NotImplementedError)

    def _get_scene_sampler(self) -> None:
        """
        Initialize the scene sampler.
        """
        self._scene_sampler: Optional[samplers.SceneSampler] = None
        if self.cfg.scene_sampler[0] == "single":
            self._scene_sampler = samplers.SingleSceneSampler(
                self.cfg.scene_sampler[1][0]
            )
        elif self.cfg.scene_sampler[0] == "subset":
            # Collect unique subset tags from config
            scene_subsets: Tuple[Set, Set] = (
                set(),
                set(),
            )  # (included, excluded) subsets
            for i in range(2):
                for scene_set in self.cfg.scene_sampler[1][i]:
                    assert (
                        scene_set in self._scene_sets
                    ), f"SubsetSceneSampler requested scene_set {scene_set} not found."
                    for scene_subset in self._scene_sets[scene_set]:
                        scene_subsets[i].add(scene_subset)

            self._scene_sampler = samplers.SceneSubsetSampler(
                scene_subsets[0], scene_subsets[1], self.sim
            )
        else:
            print(
                f"Requested scene sampler '{self.cfg.scene_sampler[0]}' is not implemented."
            )
            raise (NotImplementedError)

    def _get_ao_state_samplers(self) -> None:
        """
        Initialize and cache all ArticulatedObject state samplers from configuration.
        """
        self._ao_state_samplers: Dict[
            str, samplers.ArticulatedObjectStateSampler
        ] = {}
        for (
            ao_state_sampler_name,
            ao_state_sampler_type,
            params,
        ) in self.cfg.ao_state_samplers:
            assert (
                ao_state_sampler_name not in self._ao_state_samplers
            ), f"Duplicate AO state sampler name {ao_state_sampler_name} in config."
            if ao_state_sampler_type == "uniform":
                self._ao_state_samplers[
                    ao_state_sampler_name
                ] = samplers.ArticulatedObjectStateSampler(
                    params[0], params[1], (params[2], params[3])
                )
            elif ao_state_sampler_type == "composite":
                composite_ao_sampler_params: Dict[
                    str, Dict[str, Tuple[float, float]]
                ] = {}
                for entry in params[0]:
                    ao_handle = entry[0]
                    link_sample_params = entry[1]
                    assert (
                        ao_handle not in composite_ao_sampler_params
                    ), f"Duplicate handle '{ao_handle}' in composite AO sampler config."
                    composite_ao_sampler_params[ao_handle] = {}
                    for link_params in link_sample_params:
                        link_name = link_params[0]
                        assert (
                            link_name
                            not in composite_ao_sampler_params[ao_handle]
                        ), f"Duplicate link name '{link_name}' for handle '{ao_handle} in composite AO sampler config."
                        composite_ao_sampler_params[ao_handle][link_name] = (
                            link_params[1],
                            link_params[2],
                        )
                self._ao_state_samplers[
                    ao_state_sampler_name
                ] = samplers.CompositeArticulatedObjectStateSampler(
                    composite_ao_sampler_params
                )
            else:
                print(
                    f"Requested AO state sampler type '{ao_state_sampler_type}' not implemented."
                )
                raise (NotImplementedError)

    def _reset_samplers(self) -> None:
        """
        Reset any sampler internal state related to a specific scene or episode.
        """
        self.ep_sampled_objects = []
        self._scene_sampler.reset()
        for sampler in self._obj_samplers.values():
            sampler.reset()

    def generate_scene(self) -> str:
        """
        Sample a new scene and re-initialize the Simulator.
        Return the generated scene's handle.
        """
        cur_scene_name = self._scene_sampler.sample()
        self.initialize_sim(cur_scene_name, self.cfg.dataset_path)

        if self._render_debug_obs:
            self.visualize_scene_receptacles()
            self.vdb.make_debug_video(prefix="receptacles_")

        return cur_scene_name

    def visualize_scene_receptacles(self) -> None:
        """
        Generate a wireframe bounding box for each receptacle in the scene, aim the camera at it and record 1 observation.
        """
        print("visualize_scene_receptacles processing")
        receptacles = sutils.find_receptacles(self.sim)
        for receptacle in receptacles:
            print("receptacle processing")
            attachment_scene_node = None
            if receptacle.is_parent_object_articulated:
                attachment_scene_node = (
                    self.sim.get_articulated_object_manager()
                    .get_object_by_handle(receptacle.parent_object_handle)
                    .get_link_scene_node(receptacle.parent_link)
                    .create_child()
                )
            else:
                # attach to the 1st visual scene node so any COM shift is automatically applied
                attachment_scene_node = (
                    self.sim.get_rigid_object_manager()
                    .get_object_by_handle(receptacle.parent_object_handle)
                    .visual_scene_nodes[1]
                    .create_child()
                )
            box_obj = sutils.add_wire_box(
                self.sim,
                receptacle.bounds.size() / 2.0,
                receptacle.bounds.center(),
                attach_to=attachment_scene_node,
            )
            self.vdb.look_at(box_obj.root_scene_node.absolute_translation)
            self.vdb.get_observation()

    def generate_episodes(
        self, num_episodes: int = 1
    ) -> List[RearrangeEpisode]:
        """
        Generate a fixed number of episodes.
        """
        generated_episodes: List[RearrangeEpisode] = []
        failed_episodes = 0
        while len(generated_episodes) < num_episodes:
            new_episode = self.generate_single_episode()
            if new_episode is None:
                failed_episodes += 1
                continue
            generated_episodes.append(new_episode)

        print("==========================")
        print(
            f"Generated {num_episodes} episodes in {num_episodes+failed_episodes} tries."
        )
        print("==========================")

        return generated_episodes

    def generate_single_episode(self) -> Optional[RearrangeEpisode]:
        """
        Generate a single episode, sampling the scene.
        """

        self._reset_samplers()
        self.episode_data: Dict[str, Dict[str, Any]] = {
            "sampled_objects": {},  # object sampler name -> sampled object instances
            "sampled_targets": {},  # target sampler name -> (object, target state)
        }

        ep_scene_handle = self.generate_scene()

        # sample AO states for objects in the scene
        # ao_instance -> [ (link_ix, state), ... ]
        ao_states: Dict[
            habitat_sim.physics.ManagedArticulatedObject, Dict[int, float]
        ] = {}
        for sampler_name, ao_state_sampler in self._ao_state_samplers.items():
            sampler_states = ao_state_sampler.sample(self.sim)
            assert (
                sampler_states is not None
            ), f"AO sampler '{sampler_name}' failed"
            for sampled_instance, link_states in sampler_states.items():
                if sampled_instance not in ao_states:
                    ao_states[sampled_instance] = {}
                for link_ix, joint_state in link_states.items():
                    ao_states[sampled_instance][link_ix] = joint_state

        # sample object placements
        for sampler_name, obj_sampler in self._obj_samplers.items():
            new_objects = obj_sampler.sample(
                self.sim,
                snap_down=True,
                vdb=(self.vdb if self._render_debug_obs else None),
            )
            if sampler_name not in self.episode_data["sampled_objects"]:
                self.episode_data["sampled_objects"][
                    sampler_name
                ] = new_objects
            else:
                # handle duplicate sampler names
                self.episode_data["sampled_objects"][
                    sampler_name
                ] += new_objects
            self.ep_sampled_objects += new_objects
            print(
                f"Sampler {sampler_name} generated {len(new_objects)} new object placements."
            )
            # debug visualization showing each newly added object
            if self._render_debug_obs:
                for new_object in new_objects:
                    self.vdb.look_at(new_object.translation)
                    self.vdb.get_observation()

        # simulate the world for a few seconds to validate the placements
        if not self.settle_sim():
            print("Aborting episode generation due to unstable state.")
            return None

        #DEBUG
        # for ao in ao_states:
        #     if "fridge" in ao.handle:
        #         self.vdb.peek_object(ao, peek_all_axis=True)

        # generate the target samplers
        self._get_object_target_samplers()

        # sample targets
        for _sampler_name, target_sampler in self._target_samplers.items():
            new_target_objects = target_sampler.sample(
                self.sim, snap_down=True, vdb=self.vdb
            )
            # cache transforms and add visualizations
            for instance_handle, target_object in new_target_objects.items():
                assert (
                    instance_handle not in self.episode_data["sampled_targets"]
                ), f"Duplicate target for instance '{instance_handle}'."
                rom = self.sim.get_rigid_object_manager()
                target_bb_size = (
                    target_object.root_scene_node.cumulative_bb.size()
                )
                target_transform = target_object.transformation
                self.episode_data["sampled_targets"][
                    instance_handle
                ] = target_transform
                rom.remove_object_by_handle(target_object.handle)
                if self._render_debug_obs:
                    sutils.add_transformed_wire_box(
                        self.sim,
                        size=target_bb_size / 2.0,
                        transform=target_transform,
                    )
                    self.vdb.look_at(target_transform.translation)
                    self.vdb.debug_line_render.set_line_width(2.0)
                    self.vdb.debug_line_render.draw_transformed_line(
                        target_transform.translation,
                        rom.get_object_by_handle(instance_handle).translation,
                        mn.Color4(1.0, 0.0, 0.0, 1.0),
                        mn.Color4(1.0, 0.0, 0.0, 1.0),
                    )
                    self.vdb.get_observation()

        # collect final object states and serialize the episode
        sampled_rigid_object_states = [
            (x.creation_attributes.handle, np.array(x.transformation))
            for x in self.ep_sampled_objects
        ]

        self.num_ep_generated += 1
        # TODO: should episode_id, start_position, start_rotation be set here?
        return RearrangeEpisode(
            episode_id=str(self.num_ep_generated - 1),
            start_position=np.zeros(3),
            start_rotation=[
                0,
                0,
                0,
                1,
            ],
            scene_id=ep_scene_handle,
            ao_states=ao_states,
            rigid_objs=sampled_rigid_object_states,
            targets=self.episode_data["sampled_targets"],
            markers=self.cfg.markers,
        )

    def initialize_sim(self, scene_name: str, dataset_path: str) -> None:
        """
        Initialize a new Simulator object with a selected scene and dataset.
        """
        # Setup a camera coincident with the agent body node.
        # For debugging visualizations place the default agent where you want the camera with local -Z oriented toward the point of focus.
        camera_resolution = [540, 720]
        sensors = {
            "rgb": {
                "sensor_type": habitat_sim.SensorType.COLOR,
                "resolution": camera_resolution,
                "position": [0, 0, 0],
                "orientation": [0, 0, 0.0],
            }
        }

        backend_cfg = habitat_sim.SimulatorConfiguration()
        backend_cfg.scene_dataset_config_file = dataset_path
        backend_cfg.scene_id = scene_name
        backend_cfg.enable_physics = True

        sensor_specs = []
        for sensor_uuid, sensor_params in sensors.items():
            # sensor_spec = habitat_sim.EquirectangularSensorSpec()
            sensor_spec = habitat_sim.CameraSensorSpec()
            sensor_spec.uuid = sensor_uuid
            sensor_spec.sensor_type = sensor_params["sensor_type"]
            sensor_spec.resolution = sensor_params["resolution"]
            sensor_spec.position = sensor_params["position"]
            sensor_spec.orientation = sensor_params["orientation"]
            sensor_spec.sensor_subtype = (
                habitat_sim.SensorSubType.EQUIRECTANGULAR
            )
            sensor_spec.sensor_subtype = habitat_sim.SensorSubType.PINHOLE
            sensor_specs.append(sensor_spec)

        agent_cfg = habitat_sim.agent.AgentConfiguration()
        agent_cfg.sensor_specifications = sensor_specs

        hab_cfg = habitat_sim.Configuration(backend_cfg, [agent_cfg])
        if self.sim is None:
            self.sim = habitat_sim.Simulator(hab_cfg)
            object_attr_mgr = self.sim.get_object_template_manager()
            for object_path in self.cfg.additional_object_paths:
                object_attr_mgr.load_configs(osp.abspath(object_path))
        else:
            if self.sim.config.sim_cfg.scene_id == scene_name:
                # we need to force a reset, so change the internal config scene name
                # TODO: we should fix this to provide an appropriate reset method
                assert (
                    self.sim.config.sim_cfg.scene_id != "NONE"
                ), "Should never generate episodes in an empty scene. Mistake?"
                self.sim.config.sim_cfg.scene_id = "NONE"
            self.sim.reconfigure(hab_cfg)

        # setup the debug camera state to the center of the scene bounding box
        scene_bb = (
            self.sim.get_active_scene_graph().get_root_node().cumulative_bb
        )
        self.sim.agents[0].scene_node.translation = scene_bb.center()

        # initialize the debug visualizer
        self.vdb = sutils.DebugVisualizer(
            self.sim, output_path="rearrange_ep_gen_output/"
        )

    def settle_sim(
        self, duration: float = 5.0, make_video: bool = True
    ) -> bool:
        """
        Run dynamics for a few seconds to check for stability of newly placed objects and optionally produce a video.
        Returns whether or not the simulation was stable.
        """
        assert len(self.ep_sampled_objects) > 0

        scene_bb = (
            self.sim.get_active_scene_graph().get_root_node().cumulative_bb
        )
        new_obj_centroid = mn.Vector3()
        spawn_positions = {}
        for new_object in self.ep_sampled_objects:
            spawn_positions[new_object.handle] = new_object.translation
            new_obj_centroid += new_object.translation
        new_obj_centroid /= len(self.ep_sampled_objects)
        settle_db_obs: List[Any] = []
        if self._render_debug_obs:
            self.vdb.get_observation(
                look_at=new_obj_centroid,
                look_from=scene_bb.center(),
                obs_cache=settle_db_obs,
            )

        while self.sim.get_world_time() < duration:
            self.sim.step_world(1.0 / 30.0)
            if self._render_debug_obs:
                self.vdb.get_observation(obs_cache=settle_db_obs)

        # check stability of placements
        print("Computing placement stability report:")
        max_settle_displacement = 0
        error_eps = 0.1
        unstable_placements = []
        for new_object in self.ep_sampled_objects:
            error = (
                spawn_positions[new_object.handle] - new_object.translation
            ).length()
            max_settle_displacement = max(max_settle_displacement, error)
            if error > error_eps:
                unstable_placements.append(new_object.handle)
                print(
                    f"    Object '{new_object.handle}' unstable. Moved {error} units from placement."
                )
        print(
            f" : unstable={len(unstable_placements)}|{len(self.ep_sampled_objects)} ({len(unstable_placements)/len(self.ep_sampled_objects)*100}%) : {unstable_placements}."
        )
        print(
            f" : Maximum displacement from settling = {max_settle_displacement}"
        )
        # TODO: maybe draw/display trajectory tubes for the displacements?

        if self._render_debug_obs and make_video:
            self.vdb.make_debug_video(
                prefix="settle_", fps=30, obs_cache=settle_db_obs
            )

        # return success or failure
        return len(unstable_placements) == 0


# =======================================
# Episode Configuration
# ======================================


def get_config_defaults() -> CN:
    """
    Populates and resturns a default config for a RearrangeEpisode.
    """
    # TODO: Most of these values are for demonstration/testing purposes and should be removed.
    _C = CN()

    # ----- import/initialization parameters ------
    # the scene dataset from which scenes and objects are sampled
    _C.dataset_path = "data/replica_cad/replicaCAD.scene_dataset_config.json"
    # any additional object assets to load before defining object sets
    _C.additional_object_paths = ["data/objects/ycb/"]

    # ----- resource set definitions ------
    # define the sets of scenes which can be sampled from. [(set_name, [scene handle substrings])]
    _C.scene_sets = [
        ("default", ["v3_sc0_staging_00"]),
        ("any", [""]),
        ("v3_sc", ["v3_sc"]),
        ("original", ["apt_"]),
    ]
    # define the sets of objects which can be sampled from. [(set_name, [object handle substrings])]
    _C.object_sets = [
        (
            "any",
            [
                "002_master_chef_can",
                "003_cracker_box",
                "004_sugar_box",
                "005_tomato_soup_can",
                "007_tuna_fish_can",
                "008_pudding_box",
                "009_gelatin_box",
                "010_potted_meat_can",
                "024_bowl",
            ],
        ),
        ("cheezit", ["003_cracker_box"]),
    ]
    # define the sets of receptacles which can be sampled from. [(set_name, [receptacle object handle substrings])]
    # TODO: allow "type" to be defined to further constrain the search (e.g. "surface" or "drawer").
    _C.receptacle_sets = [
        ("table", ["frl_apartment_table_01"]),
        ("table3", ["frl_apartment_table_03"]),
        (
            "any",
            [
                "frl_apartment_table_01",
                "frl_apartment_table_02",
                "frl_apartment_table_03",
                "frl_apartment_chair_01",
            ],
        ),
        ("fridge", ["fridge"]),
    ]

    # ----- sampler definitions ------
    # define the desired scene sampling (sampler type, (sampler parameters tuple))
    # NOTE: There must be exactly one scene sampler!
    # "single" scene sampler params ("scene name")
    _C.scene_sampler = ("single", ("v3_sc0_staging_00",))
    # "subset" scene sampler params ([included scene sets], [excluded scene sets])
    # _C.scene_sampler = ("subset", (["v3_sc"], []))

    # define the desired object sampling [(name, sampler type, (sampler parameters tuple))]
    _C.obj_samplers = [
        # (name, type, (params))
        # - uniform sampler params: ([object sets], [receptacle sets], min samples, max samples, orientation_sampling)
        # ("cheezits", "uniform", (["cheezit"], ["table"], 3, 3, "up"))
        # ("cheezits", "uniform", (["cheezit"], ["table"], 3, 5, "up")),
        # ("any", "uniform", (["any"], ["any"], 3, 5, "up")),
        ("any", "uniform", (["any"], ["any"], 20, 50, "up")),
        # ("fridge", "uniform", (["any"], ["fridge"], 20, 50, "up")),
        ("fridge", "uniform", (["any"], ["fridge"], 1, 30, "up")),
    ]
    # define the desired object target sampling (i.e., where should an existing object go)
    _C.obj_target_samplers = [
        # (name, type, (params))
        # - uniform target sampler params: ([obj sampler name(s)], [receptacle sets], min targets, max targets, orientation_sampling)
        # ("any_targets", "uniform", (["any"], ["table"], 3, 3, "up"))
    ]
    # define ArticulatedObject(AO) joint state sampling (when a scene is initialized, all samplers are run for all matching AOs)
    _C.ao_state_samplers = [
        # (name, type, (params))
        # TODO: does not support spherical joints (3 dof joints)
        # - uniform continuous range for a single joint. params: ("ao_handle", "link name", min, max)
        # ("open_fridge_top_door", "uniform", ("fridge", "top_door", 1.5, 1.5)),
        # ("variable_fridge_bottom_door", "uniform", ("fridge", "bottom_door", 1.5, 1.5))
        # composite sampler (rejection sampling of composite configuration)
        # params: ([("ao handle", [("link name", min, max)])])
        # NOTE: the trailing commas are necessary to define tuples of 1 object
        (
            "open_fridge_doors",
            "composite",
            (
                [
                    (
                        "fridge",
                        [("top_door", 1.5, 1.5), ("bottom_door", 1.5, 1.5)],
                    ),
                ],
            ),
        )
    ]

    # ----- marker definitions ------
    # a marker defines a point in the local space of a rigid object or articulated link which can be registered to instances in a scene and tracked
    _C.markers = [
        # (name, type, (params))
        # articulated_object type params: ("ao_handle", "link_name", (vec3 local offset)))
        (
            "fridge_push_point",
            "articulated_object",
            ("fridge", "bottom_door", (0.1, -0.62, -0.2)),
        ),
        # rigid_object type params: ("object_handle", (vec3 local offset)))
        (
            "chair",
            "rigid_object",
            ("frl_apartment_chair_01", (0.1, -0.62, -0.2)),
        ),
    ]

    return _C.clone()