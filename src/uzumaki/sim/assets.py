"""Pinned robot models from MuJoCo Menagerie, fetched on demand and verified by SHA-256.

Each ``PinnedModel`` is one Menagerie directory at a fixed commit, with the SHA-256 of every file
its scene loads and of its LICENSE:

- ``so101``: ``robotstudio_so101`` (Apache-2.0);
- ``panda``: ``franka_emika_panda`` (Apache-2.0);
- ``aloha``: ``aloha`` (BSD-3-Clause; the notice ships in its LICENSE file).

Files are never committed to this repository: ``fetch`` downloads them into a cache directory and
every load verifies each file first, so a changed or partial download is refused instead of
silently simulating a different robot. The cache is ``$UZUMAKI_<NAME>_ASSETS`` if set (for
example ``UZUMAKI_SO101_ASSETS``), otherwise ``~/.cache/uzumaki/<name>-<commit>``.

Usage: ``python -m uzumaki.sim.assets fetch|verify [NAME ...]`` (default: ``so101``). The
module-level ``FILES``, ``verify_assets`` and friends are the SO-101's, as before.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from uzumaki.sim import SimUnavailable

MENAGERIE_COMMIT = "cda5236f98383f6fbaa4820c6b18dba892cd087f"
MODEL_DIR = "robotstudio_so101"
SOURCE_URL = (
    f"https://raw.githubusercontent.com/google-deepmind/mujoco_menagerie/{MENAGERIE_COMMIT}/"
    f"{MODEL_DIR}/"
)
ENV_VAR = "UZUMAKI_SO101_ASSETS"
MAX_FILE_BYTES = 8 << 20

#: Relative path -> SHA-256 of every file the scene loads, plus the model's license.
FILES: Mapping[str, str] = MappingProxyType(
    {
        "so101.xml": "982c02c027ec0c62d3459d0a6eb00dfb7dc13a451992e72ce99addc71f209805",
        "LICENSE": "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
        "assets/base_motor_holder_so101_v1.stl": (
            "8cd2f241037ea377af1191fffe0dd9d9006beea6dcc48543660ed41647072424"
        ),
        "assets/base_so101_v2.stl": (
            "bb12b7026575e1f70ccc7240051f9d943553bf34e5128537de6cd86fae33924d"
        ),
        "assets/motor_holder_so101_base_v1.stl": (
            "31242ae6fb59d8b15c66617b88ad8e9bded62d57c35d11c0c43a70d2f4caa95b"
        ),
        "assets/motor_holder_so101_wrist_v1.stl": (
            "887f92e6013cb64ea3a1ab8675e92da1e0beacfd5e001f972523540545e08011"
        ),
        "assets/moving_jaw_so101_gripper_part0_v1.stl": (
            "3a92e115d1934d4c45cff808c4eff2ea10361a137a7f89940033a1f8a821b88c"
        ),
        "assets/moving_jaw_so101_gripper_part1_v1.stl": (
            "d10f6c671d26f62988ec95efee9370a0657b5833b3c5f8aad59fd69a87ef2aee"
        ),
        "assets/moving_jaw_so101_gripper_v1.stl": (
            "e8a5dc040b98e27453a5b74f9087d1dbca14589cf9a17c4de7868d5ab98413cd"
        ),
        "assets/moving_jaw_so101_v1.stl": (
            "785a9dded2f474bc1d869e0d3dae398a3dcd9c0c345640040472210d2861fa9d"
        ),
        "assets/rotation_pitch_so101_v1.stl": (
            "9be900cc2a2bf718102841ef82ef8d2873842427648092c8ed2ca1e2ef4ffa34"
        ),
        "assets/sts3215_03a_no_horn_v1.stl": (
            "75ef3781b752e4065891aea855e34dc161a38a549549cd0970cedd07eae6f887"
        ),
        "assets/sts3215_03a_v1.stl": (
            "a37c871fb502483ab96c256baf457d36f2e97afc9205313d9c5ab275ef941cd0"
        ),
        "assets/under_arm_so101_v1.stl": (
            "d01d1f2de365651dcad9d6669e94ff87ff7652b5bb2d10752a66a456a86dbc71"
        ),
        "assets/upper_arm_so101_v1.stl": (
            "475056e03a17e71919b82fd88ab9a0b898ab50164f2a7943652a6b2941bb2d4f"
        ),
        "assets/waveshare_mounting_plate_so101_v2.stl": (
            "e197e24005a07d01bbc06a8c42311664eaeda415bf859f68fa247884d0f1a6e9"
        ),
        "assets/wrist_roll_follower_so101_camera_mount.stl": (
            "58c457b33a26d9c9bb337aa351173e7123f84719a93f486541adef6eba57ae21"
        ),
        "assets/wrist_roll_follower_so101_gripper_part0_v1.stl": (
            "370eb470a6ab48b08febcaeb5274d2bcc91246692532ce12c795fb82977def72"
        ),
        "assets/wrist_roll_follower_so101_gripper_v1.stl": (
            "65f32debcf265420c0160afe004f8befc5df4b3e578fd59ebaa62eade0371224"
        ),
        "assets/wrist_roll_follower_so101_v1.stl": (
            "4b17b410a12d64ec39554abc3e8054d8a97384b2dc4a8d95a5ecb2a93670f5f4"
        ),
        "assets/wrist_roll_pitch_so101_v2.stl": (
            "6c7ec5525b4d8b9e397a30ab4bb0037156a5d5f38a4adf2c7d943d6c56eda5ae"
        ),
    }
)


PANDA_FILES: Mapping[str, str] = MappingProxyType(
    {
        "LICENSE": "a6cba85bc92e0cff7a450b1d873c0eaa2e9fc96bf472df0247a26bec77bf3ff9",
        "assets/finger_0.obj": "215098c0fcdf7192f6c503a37cf49e3efecf8a9e1320f02b17c5584d89cc5b2c",
        "assets/finger_1.obj": "cf239e9aca26bba09dbbf1c544c47d87cfc9a2f0637c8bb786af750b6d4eca44",
        "assets/hand.stl": "94493e94f30fe940f2c8ca2f155c3bbe67bbff406d3edf5e261670d2f0f6e2ed",
        "assets/hand_0.obj": "b94aab901086f85df04c019b30f43bf9aa23a7ad0443ec80a8c14662a41cabd2",
        "assets/hand_1.obj": "cd76f7494bb314406a1819c46eb826ee254894f87235714f604a470653d9badb",
        "assets/hand_2.obj": "e0f44a71712afa5b98eb20355eff17763d3e2e19f459b2bef19abed3727e64b3",
        "assets/hand_3.obj": "a431a06d99e88a1b322b3122a01424ee7704daeaad0bc343b90b9604f014ad0c",
        "assets/hand_4.obj": "21f96ef6a8de082e41b1ea9834f1043de359e5d60305541bcb63539581f53bff",
        "assets/link0.stl": "dfc6d94330de8ddb005b311bfdba9f3b8e1aa7c256b71592ee7ff32cb9a9a5aa",
        "assets/link0_0.obj": "6b9f41a4c203540daaeb7a33cc3d0d6c6a32dc8bba187074eeaf97b3e36b3f9c",
        "assets/link0_1.obj": "57673d2ee2a9db2e10c8986ae57c56a7c39c012b60fd26d7d8549553b664a5ed",
        "assets/link0_10.obj": "c9198b9d8534d628d097b0f2b239bb30343353182df098ce0d9db6bc13326848",
        "assets/link0_11.obj": "f0adec78d6677b7799d25121981270e8bfe119b5c18d08a91ee37a54ad75f65c",
        "assets/link0_2.obj": "1ca435a2a87a9608a146a47955724d43dd7ce10fc3dd3018800aa98f8ba86e05",
        "assets/link0_3.obj": "3e9c6334d2b3b47944af3458ee5f20519abc20fe7ee23063bb53405ccab28046",
        "assets/link0_4.obj": "215d25789704936120f119c0bc1cc7f68516dd3f8f1df61457b71acac3873f26",
        "assets/link0_5.obj": "9361b104e93743045107aa5b15fb1a0835ba9c1f355aae71f56403cf6a9c33f6",
        "assets/link0_7.obj": "2dc0b8ebbbc80e4c065d71e195adbc384f673438641fac82b07b801b4d2837cb",
        "assets/link0_8.obj": "3b8c436999db5586b8eb47bed2b0aa7de6993868b2681b4c04246eeac42fdb7a",
        "assets/link0_9.obj": "45178f97d2b5da6bce11f610212c4ef7d08c73db33a4fc38e1b7f66b93d023f5",
        "assets/link1.obj": "51091e776b16babdbe2f3849b5c041f921d1f11dd94ac19a0f1c81abf496fbae",
        "assets/link1.stl": "e41a39a94108fcf56aacff603fc91ec80541f4c1af17b51a0de5617f5566e6d2",
        "assets/link2.obj": "5c9f8e715de52c3eb71b475de35993b3b96081e02abea9f28c414768877fc59b",
        "assets/link2.stl": "370f7605a0fae3529db169ded50f52f171024aa792d4d773bc84197301f6a039",
        "assets/link3.stl": "0a8d638b9349c6c0eefc4e888636ac4838c4b27170f18a51699321118af709c1",
        "assets/link3_0.obj": "ce2da87f51708e11b189249d41401065dabe89a06e251185a06cef4d5ff27178",
        "assets/link3_1.obj": "2880b7753a7173f38fa87ee21da4dfc7fa7be46fbe07bac5aef9e128513cb118",
        "assets/link3_2.obj": "670cff81ae84693769ca4ae658c154cf909d81167e7c8d22b70ea977f2504efa",
        "assets/link3_3.obj": "30ede0c812efd0e0b767d134a4eae0b84e0fa407bb6325a5a555e0569b3a3ffd",
        "assets/link4.stl": "0180ebb5772ec9840cb049750cffb29a9ddc90311752a16ea34757782ef9e48d",
        "assets/link4_0.obj": "443bf6d178177cf347951ecf107ccd10ca444e605e375b67304579e5f948806f",
        "assets/link4_1.obj": "ca935ec7cc8b3f0e86aa079c76b0b3bf593b1e9830bab0fa7131ac51577c74f0",
        "assets/link4_2.obj": "c89caf26d379c35bb9e191ee2d91d09ab29ecc792b48d8bc10b39e5b38222175",
        "assets/link4_3.obj": "1495d17f45bdb230cf58b0beef9da4e8c9cfe76a3d2d38da89017efe5e093136",
        "assets/link5_0.obj": "a3238b8f2af0f2eb66d9fe63dc5d41ac52d5d38507241b5ed10e3cb019ab3e71",
        "assets/link5_1.obj": "9d994bbf83ec241e301b5197a6846da13f2a4a0582edb2c0f76962c5549649d6",
        "assets/link5_2.obj": "83b4ffe9f772cd7fe182e98640650eebc899aaf01842656574b0416c90dd40a4",
        "assets/link5_collision_0.obj": (
            "3e5aa75fb13f193555c3dfb999df91d81df25006a5c3af5338297d78137c129b"
        ),
        "assets/link5_collision_1.obj": (
            "87cd8f621071df010f774cbde789282b8463840dfe704f8bdfa2e29be0142e15"
        ),
        "assets/link5_collision_2.obj": (
            "823ab6c8f7b1d79e0844fa071d82a03499ff68e34fddb1ba3451032c22736040"
        ),
        "assets/link6.stl": "20b768e99a0e0440b5754dcca108016434e57937cc356acd9c352ccd3cb27f77",
        "assets/link6_0.obj": "0504c70ebf1b284ee8faae7dcb896b8830bc672a6552015dd5dbd121fa031fb9",
        "assets/link6_1.obj": "45f7ca77695198b6d86ec2417ad4e42e6275044f0f96266699ee8e1057757ac1",
        "assets/link6_10.obj": "365f26c21d10332cab8d8704645cb46930c30b3085956f2d097498c793f24db4",
        "assets/link6_11.obj": "8ae61e08b3ad064f25bbf03b22e59cc15d65883b5a7ce92711aff29df2493901",
        "assets/link6_12.obj": "f28d1bfd62b0244ffd19fb35fe9edefdf583d3c9167db944e87185d919e1d021",
        "assets/link6_13.obj": "0a55fa92dccb261d07200e6565fca4c850fd17f0c1ecf38821c0b97d51796c7d",
        "assets/link6_14.obj": "750ae3ee4781e62e3a045c662195247b03f6642b05aa93104f08258e5f6eba26",
        "assets/link6_15.obj": "5999c1c2497a004a7c4bbe034cdf13744f839d85ae7a11b1bc386c48a3e642e6",
        "assets/link6_16.obj": "551d7a8b37c53e7a152d008b4e36992e5b5abe74a4be18b57ee6319c27def09f",
        "assets/link6_2.obj": "e54eee4d8232947ee8af65afd5a067e09dac2da6e7e585cb1dccb65496457da0",
        "assets/link6_3.obj": "d4f6a25ef32fa1fa4734146981994ee760232cb077c80fea6487d78a077c7f27",
        "assets/link6_4.obj": "be353480fe71d46f7a96f0a3cbb7e112caaf8d88368826f321a36a253c8749b2",
        "assets/link6_5.obj": "7c87664623c3e1a6cb330db273227a69626dc5e53b4caff1d4ee7911eb3340c6",
        "assets/link6_6.obj": "8fbc031e0d07b795ebf99115c75223141caaba317cab569159f3d477a7821681",
        "assets/link6_7.obj": "7dc5ebb799fe281aab27d6e84e7a4efa0a8e42da2369ec83de3ea3f206f62d73",
        "assets/link6_8.obj": "e3f77381817a341030f9dcc4c4aa7c559f80411b453fd87eb0fefecca82151bf",
        "assets/link6_9.obj": "25a56ea9b02a03b6cb5a81f31c54f54d0185e1a0b3937c37e4289d896e50215a",
        "assets/link7.stl": "92ac6afcf7574c034d3170d8a68e95ac9048ab9d0dd5bbd8311b86e551b9ab1c",
        "assets/link7_0.obj": "04586b0bde011715cfc22072ac72fe0717eb4d4817bcf665987ce6cd88eb2b93",
        "assets/link7_1.obj": "c3f72289f394c9d6749a4a327fb11f05b203c37c95032c92f28019b40ebe7e00",
        "assets/link7_2.obj": "d2faecedb304d0989040d082410378dc949dbbda23bd18c05134f08aae984db5",
        "assets/link7_3.obj": "b62d87c4d4bdb7b281ed341d347c39dad0e50bf36aed8ac11d050073701d5f70",
        "assets/link7_4.obj": "cb78ccb61fd4ad6c7bff4c4d2e6cb41e1964a5a3e1a28e67c0c6202371cfc018",
        "assets/link7_5.obj": "099071d3090b51471ba52f4f90814fd3089f4975d196e4d488117073d0bfe491",
        "assets/link7_6.obj": "99469e2d95ab74ce0292f2f16954c87a8a7dc16652e62e970fe37c94330a81bb",
        "assets/link7_7.obj": "426c3db8a19373f3dc73d4bec672e948121b2914c10e2e7b9c7ea80643a19e54",
        "panda.xml": "96ad67da03710f17f798c9478fd9e9efdf24a3bf8359f05e456dd9fb158ea273",
    }
)

ALOHA_FILES: Mapping[str, str] = MappingProxyType(
    {
        "LICENSE": "21868447315ac2b10e592d08958c1035b19a32cf39e9165b23033f68390098cb",
        "aloha.xml": "68430b29719bda1b75e63f540953f81991bc3fd136bdf0a43bbe3e04393b78d3",
        "assets/d405_solid.stl": "8a9a84dd9c9a67687e5ad0b37a7ed334dbfea6b0f995fef28524cc8da5f21ed2",
        "assets/vx300s_1_base.stl": (
            "d100cafe656671ca8fde98fb6a4cf2d1b746995c51c61c25ad9ea2715635d146"
        ),
        "assets/vx300s_2_shoulder.stl": (
            "139745a74055cb0b23430bb5bc032bf68cf7bea5e4975c8f4c04107ae005f7f0"
        ),
        "assets/vx300s_3_upper_arm.stl": (
            "900f236320dd3d500870c5fde763b2d47502d51e043a5c377875e70237108729"
        ),
        "assets/vx300s_4_upper_forearm.stl": (
            "4104fc54bbfb8a9b533029f1e7e3ade3d54d638372b3195daa0c98f57e0295b5"
        ),
        "assets/vx300s_5_lower_forearm.stl": (
            "66814e27fa728056416e25e02e89eb7d34c51d51c51e7c3df873829037ddc6b8"
        ),
        "assets/vx300s_6_wrist.stl": (
            "90eb145c85627968c3776ae6de23ccff7e112c9dd713c46bc9acdfdaa859a048"
        ),
        "assets/vx300s_7_gripper_bar.stl": (
            "b76420544e84102b1cd30745f3c6c2675f9bf08a656808101f32b6466a33f2ee"
        ),
        "assets/vx300s_7_gripper_prop.stl": (
            "4536e8975278953841f25391dccef10907859e69c71b9f06f794fef6dfc5959c"
        ),
        "assets/vx300s_7_gripper_wrist_mount.stl": (
            "50c54e0486d3e349785163dbce209313f7b4b3feadc252ab2e2c9e55a5d0e7ef"
        ),
        "assets/vx300s_8_custom_finger_left.stl": (
            "5853af5b66dd9fc954b6705a883d8569c96536d86775d094f3670befaff67a93"
        ),
        "assets/vx300s_8_custom_finger_right.stl": (
            "9a1bbd942b887f27e2680b4c99dd3b3d2f73578fa9a87cc0f4dd1da3cf88b2e2"
        ),
        "joint_position_actuators.xml": (
            "ce843cf0cfecea5ec1f117eb4257d4249dc2296e92e5f2ffb40f2e0178109bc6"
        ),
        "keyframe_ctrl.xml": "960481d23320fb45cd1cdaa75fb48ec69fa0dec46851dabcfac7f00a583c8a45",
    }
)


#: Stanford TidyBot (MIT): a holonomic base with a Kinova Gen3 arm and a Robotiq 2F-85 gripper.
#: The arm and gripper come from Menagerie's ``kinova_gen3`` and ``robotiq_2f85`` (both
#: BSD-3-Clause), whose license notices are pinned with it.
TIDYBOT_FILES: Mapping[str, str] = MappingProxyType(
    {
        "LICENSE": "2beab4272f26da102a26b0e28f59525584278bca36631a4ce9363595839a6981",
        "tidybot.xml": "cf50d7ad343629475b9cd420a1e98fd68417ebf843956960906bf7e179d6b95e",
        "assets/base/arm_plate.stl": (
            "373ce9aba1204fd58f1a5c1d0c48837e3294bb681e9bf7279830b1bb59c5c31c"
        ),
        "assets/base/body.stl": "5c14e25d607f2fdb25edb61da8caac79d12acaa9a0f06571eaa22de9b7f34b59",
        "assets/base/bottom_plate.stl": (
            "329c784ee2e87155c9d85026586ccc524b089b59608ec3240e2350a73835cef2"
        ),
        "assets/base/bumper.stl": (
            "712604bd2164514772c83275001aecb98adb57698a09692ce8614ddc28a3d163"
        ),
        "assets/base/top_plate.stl": (
            "57d77eba37836197375bb5d0c420f1ea3f128f45a1147f1923b5090833e83177"
        ),
        "assets/gen3/base_link.stl": (
            "3e38517e653d58f4160cfdcee34c9dea983d8b3ecb8f2c147444f58235335260"
        ),
        "assets/gen3/shoulder_link.stl": (
            "a98e232e5197f09e9cd520e7a450c21bcec74bc49e2a19ce7cf86c5dc25f2e18"
        ),
        "assets/gen3/half_arm_1_link.stl": (
            "8df461a71780d8e2d09104a507584424279d85173adf14867e359f7e58b3bba7"
        ),
        "assets/gen3/half_arm_2_link.stl": (
            "8a9f38de21648804b3ab7076ec73726031f2ade7c95eb2541d070553e025407b"
        ),
        "assets/gen3/forearm_link.stl": (
            "a00d96d2c7ec37e9fe4c5fb6d3fa2b4386dfd48fea5ac25632f4cb83b3505105"
        ),
        "assets/gen3/spherical_wrist_1_link.stl": (
            "0b97a5e3b2de535344f8e8d4176d77f0cdb3455b235f35430fa0413208856e66"
        ),
        "assets/gen3/spherical_wrist_2_link.stl": (
            "12271fb7d0ee78fb5642424cf5078be20f0476061e74203db964b674031ce879"
        ),
        "assets/gen3/bracelet_with_vision_link.stl": (
            "8bb05e420a88532b4eee3948a0047fb8e7f341c3e5cc971d54e97e2aaffcf878"
        ),
        "assets/2f85/base.stl": "1c7b9f2bd92d705fc4e897c94e905973a3c05f406845e942229433deb7041453",
        "assets/2f85/coupler.stl": (
            "d5ee95e62f8415bf5b6e503c831a958f5fc1990bf9b2865329ec38a28932727c"
        ),
        "assets/2f85/driver.stl": (
            "bbd6e868b1778bead60d2516c7ede581d7ad4e431744b1828757d6ea5129c112"
        ),
        "assets/2f85/follower.stl": (
            "633793332c081641ce200df27a40643cc293b29956e3cb5cb29cb811c33ef1c7"
        ),
        "assets/2f85/pad.stl": "4af1fa8d9bb285abecbf5dfc88e74cb30f059a7a074c2030dc35e9ee4316019d",
        "assets/2f85/silicone_pad.stl": (
            "ca20c0fa61e6d3ce7b04bed25360e048e31dfd88ace04187aa6d9e8f4ca3fd0f"
        ),
        "assets/2f85/spring_link.stl": (
            "4fb74a8a0d76c0e471cf19fd48bc676fd5b19123798e7a39eb6aa56869354283"
        ),
    }
)
TIDYBOT_NOTICES: Mapping[str, str] = MappingProxyType(
    {
        "kinova_gen3/LICENSE": "50ee431dbfbfcc53d976d3a5a2918e2fff4236f7f3c492d405e98a3f72106736",
        "robotiq_2f85/LICENSE": "3d5edd18d1e22a17666e9bb84270171ed5ac90b192d24e0177fd171708ea2316",
    }
)


@dataclass(frozen=True)
class PinnedModel:
    """One Menagerie model directory at ``MENAGERIE_COMMIT`` with its pinned files."""

    name: str
    directory: str
    files: Mapping[str, str]
    entry: str
    #: License notices of parts this model includes from other Menagerie directories: path from
    #: the Menagerie root -> SHA-256, kept in the cache under ``notices/<path>``.
    notices: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def env_var(self) -> str:
        return f"UZUMAKI_{self.name.upper()}_ASSETS"

    @property
    def source_url(self) -> str:
        return (
            "https://raw.githubusercontent.com/google-deepmind/mujoco_menagerie/"
            f"{MENAGERIE_COMMIT}/{self.directory}/"
        )

    def manifest_sha256(self) -> str:
        lines = [MENAGERIE_COMMIT, *(f"{n} {d}" for n, d in sorted(self._pinned().items()))]
        return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()

    def _pinned(self) -> dict[str, str]:
        """Cache-relative path -> SHA-256 of every pinned file, notices included."""
        return {**self.files, **{f"notices/{n}": d for n, d in self.notices.items()}}

    def _url(self, name: str) -> str:
        if name.startswith("notices/"):
            root = self.source_url.removesuffix(f"{self.directory}/")
            return root + name.removeprefix("notices/")
        return self.source_url + name

    def asset_dir(self) -> Path:
        configured = os.environ.get(self.env_var)
        if configured:
            return Path(configured)
        return Path.home() / ".cache" / "uzumaki" / f"{self.name}-{MENAGERIE_COMMIT[:12]}"

    def verify(self, directory: Path | None = None) -> Path:
        """The asset directory if every pinned file is present and matches its hash; raises
        ``SimUnavailable`` naming the first missing or mismatched file otherwise."""
        root = self.asset_dir() if directory is None else directory
        for name, digest in self._pinned().items():
            path = root / name
            if not path.is_file():
                raise SimUnavailable(
                    f"{self.label} asset missing: {name} "
                    f"(run: python -m uzumaki.sim.assets fetch {self.name})"
                )
            if _sha256(path) != digest:
                raise SimUnavailable(f"{self.label} asset does not match its pinned hash: {name}")
        return root

    def fetch(self, directory: Path | None = None) -> Path:
        """Download every pinned file that is missing or wrong, verify it, then the set."""
        root = self.asset_dir() if directory is None else directory
        for name, digest in self._pinned().items():
            path = root / name
            if path.is_file() and _sha256(path) == digest:
                continue
            with urllib.request.urlopen(self._url(name), timeout=60) as response:  # noqa: S310
                data = response.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != digest:
                raise SimUnavailable(
                    f"downloaded {self.label} asset does not match its pinned hash: {name}"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                handle.write(data)
            os.replace(handle.name, path)
        return self.verify(root)

    @property
    def label(self) -> str:
        labels = {"so101": "SO-101", "panda": "Panda", "aloha": "ALOHA", "tidybot": "TidyBot"}
        return labels.get(self.name, self.name)


SO101_MODEL = PinnedModel("so101", MODEL_DIR, FILES, "so101.xml")
PANDA_MODEL = PinnedModel("panda", "franka_emika_panda", PANDA_FILES, "panda.xml")
ALOHA_MODEL = PinnedModel("aloha", "aloha", ALOHA_FILES, "aloha.xml")
TIDYBOT_MODEL = PinnedModel(
    "tidybot", "stanford_tidybot", TIDYBOT_FILES, "tidybot.xml", TIDYBOT_NOTICES
)
MODELS: Mapping[str, PinnedModel] = MappingProxyType(
    {m.name: m for m in (SO101_MODEL, PANDA_MODEL, ALOHA_MODEL, TIDYBOT_MODEL)}
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_sha256() -> str:
    """One digest over the commit and every pinned SO-101 file hash (recorded with evidence)."""
    return SO101_MODEL.manifest_sha256()


def asset_dir() -> Path:
    """The configured SO-101 asset cache directory (it may not exist yet)."""
    return SO101_MODEL.asset_dir()


def verify_assets(directory: Path | None = None) -> Path:
    """The SO-101 asset directory if every pinned file is present and matches its hash."""
    return SO101_MODEL.verify(directory)


def fetch_assets(directory: Path | None = None) -> Path:
    """Download and verify the SO-101 assets."""
    return SO101_MODEL.fetch(directory)


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("fetch", "verify") or not set(argv[1:]) <= set(MODELS):
        names = "|".join(MODELS)
        print(f"usage: python -m uzumaki.sim.assets fetch|verify [{names} ...]", file=sys.stderr)
        return 2
    for name in argv[1:] or ["so101"]:
        model = MODELS[name]
        try:
            root = model.fetch() if argv[0] == "fetch" else model.verify()
        except SimUnavailable as error:
            print(error, file=sys.stderr)
            return 1
        print(f"{model.label} assets verified in {root} (menagerie {MENAGERIE_COMMIT[:12]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
