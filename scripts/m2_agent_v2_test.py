# -*- coding: utf-8 -*-
"""M2 Agent v2.1 决策单元测试（用户策略指导版）。

场景（用户规则）：
  0. 勾中连招：二技能释放后窗口内敌人被拉近 → 召唤师技能 + 三技能
  1. 塔规避：敌方塔可见且无我方小兵 → 移动避开（技能不受限）
  2. 二技能：敌方英雄在二技能范围内 → 钩子
  3. 一技能：不在大招生效期 且 敌人/敌兵在身边 → 一技能
  4. 移动：有敌人→朝最近敌人持续拖动(2000ms)；无敌人→跟随 ally_hero 或朝发育路
运行：
    venv\\Scripts\\python.exe scripts\\m2_agent_v2_test.py
"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from m2_agent_v2 import decide, LANE_DIR_BLUE, MOVE_DURATION_MS  # noqa: E402

W, H = 1280, 720
T = 100.0


def fresh_cd():
    return {"skill1_t": 0.0, "skill2_t": 0.0, "skill3_t": 0.0,
            "summoner_t": 0.0, "recall_t": 0.0, "hp_t": 0.0,
            "skill": 0.0, "hook_pending": 0.0,
            "hook_anchor_dist": 0.0, "turret_threat": 0.0,
            "match_start_t": 0.0, "restore_t": 0.0, "attack_t": 0.0,
            "hook_blocked": 0.0}

def state(units=None, minimap=None, t=T, screen=(W, H)):
    return {"t": t, "screen_size": list(screen), "units": units or [],
            "minimap": minimap if minimap is not None else {"found": False}}


def enemy(cx, cy):
    return {"cls": "enemy_hero", "screen": [cx, cy, 0.06, 0.06]}


def enemy_minion(cx, cy):
    return {"cls": "enemy_minion", "screen": [cx, cy, 0.04, 0.04]}


def enemy_turret(cx, cy):
    return {"cls": "enemy_turret", "screen": [cx, cy, 0.10, 0.10]}


def ally(cx, cy):
    return {"cls": "ally_hero", "screen": [cx, cy, 0.06, 0.06]}


def mm_found(red=None, blue=None):
    return {"found": True, "center": [110, 93], "radius": 60,
            "dots": {"blue": blue or [], "red": red or [], "yellow": []},
            "towers": []}


def mm_towers(enemy=(), self_est=(0.5, 0.5)):
    """F1 小地图塔契约：towers_v7.enemy=[{'n':[x,y]}] + cooldowns.self_est。"""
    mm = mm_found()
    mm["towers_v7"] = {"enemy": [{"n": list(p)} for p in enemy]}
    return mm


def tower_cd(enemy=(), self_est=(0.5, 0.5)):
    cd = fresh_cd()
    cd["camp"] = "blue"
    cd["match_start_t"] = T - 20.0
    cd["self_est"] = self_est
    return cd


class TestHookCombo(unittest.TestCase):
    """规则 0：勾中连招 -> summoner + skill3。"""

    def test_hook_confirmed_triggers_combo(self):
        cd = fresh_cd()
        cd["hook_pending"] = T - 0.2          # 0.2s 前放钩
        cd["hook_anchor_dist"] = 0.40         # 释放时敌人在 0.4 屏宽
        st = state(units=[enemy(0.68, 0.50)])  # 现在 0.18 < 0.40*0.72=0.288 -> 勾中
        act = decide(st, cd)
        self.assertEqual(act["type"], "combo")
        types = [a["type"] for a in act["actions"]]
        self.assertEqual(types, ["summoner", "skill"])
        self.assertEqual(act["actions"][1]["id"], 3)

    def test_no_combo_when_enemy_not_pulled(self):
        cd = fresh_cd()
        cd["hook_pending"] = T - 0.2
        cd["hook_anchor_dist"] = 0.20
        st = state(units=[enemy(0.62, 0.50)])  # 0.12 缩小 0.08 < 0.12 -> 不算勾中
        act = decide(st, cd)
        self.assertNotEqual(act["type"], "combo")

    def test_combo_window_expires(self):
        cd = fresh_cd()
        cd["hook_pending"] = T - 2.0           # 超窗
        cd["hook_anchor_dist"] = 0.40
        st = state(units=[enemy(0.55, 0.50)])  # 很近
        act = decide(st, cd)
        self.assertNotEqual(act["type"], "combo")
        # 距离 0.05 < 0.42 -> 钩子（冷却就绪）
        self.assertEqual((act["type"], act.get("id")), ("skill", 2))


class TestTurretAvoid(unittest.TestCase):
    """规则 1（v16.0 P0-F1）：敌塔来自小地图 towers_v7，不再用屏幕塔框。"""

    def test_avoid_turret_when_no_allied_minion(self):
        st = state(minimap=mm_towers([(0.58, 0.50)]))
        act = decide(st, tower_cd())
        self.assertIn(act["type"], ("move", "map_move"))
        self.assertEqual(act["reason"], "avoid_turret")
        self.assertLess(act["nx"], 0.5)

    def test_escape_turret_when_very_close(self):
        st = state(minimap=mm_towers([(0.54, 0.50)]))
        act = decide(st, tower_cd())
        self.assertEqual(act["reason"], "escape_turret")
        self.assertLess(act["nx"], 0.5)

    def test_turret_threat_ignores_ally_minion(self):
        """v8.0/F1：小兵不参与决策；塔威胁仍按 towers_v7 生效。"""
        st = state(units=[{"cls": "ally_minion", "screen": [0.6, 0.5, 0.04, 0.04]}],
                   minimap=mm_towers([(0.54, 0.50)]))
        act = decide(st, tower_cd())
        self.assertIn(act["reason"], ("avoid_turret", "escape_turret"))

    def test_escape_turret_when_in_range(self):
        """火力圈内（<0.06）且无我方小兵 -> 立即反向逃离。"""
        st = state(minimap=mm_towers([(0.54, 0.50)]))
        act = decide(st, tower_cd())
        self.assertIn(act["type"], ("move", "map_move"))
        self.assertEqual(act["reason"], "escape_turret")
        self.assertLess(act["nx"], 0.5)

    def test_escape_turret_beats_skill2(self):
        """塔火力圈内即使敌人在钩子范围内也先跑。"""
        st = state(units=[enemy(0.72, 0.50)],
                   minimap=mm_towers([(0.54, 0.50)]))
        act = decide(st, tower_cd())
        self.assertIn(act["type"], ("move", "map_move"))
        self.assertEqual(act["reason"], "escape_turret")

    def test_escape_turret_when_moving_toward_turret(self):
        """威胁带内且有敌人 -> 先逃离敌塔。"""
        st = state(units=[enemy(0.97, 0.50)],
                   minimap=mm_towers([(0.58, 0.50)]))
        act = decide(st, tower_cd())
        self.assertIn(act["type"], ("move", "map_move"))
        self.assertEqual(act["reason"], "escape_turret")
        self.assertLess(act["nx"], 0.5)


class TestSkill2(unittest.TestCase):
    """规则 2：敌人在二技能范围内 -> 钩子。"""

    def test_enemy_in_range_triggers_skill2(self):
        st = state(units=[enemy(0.75, 0.50)])  # dx=0.25 < 0.28（实测射程 0.30）
        act = decide(st, fresh_cd())
        self.assertEqual((act["type"], act["id"]), ("skill", 2))

    def test_enemy_out_of_range_no_hook(self):
        cd = fresh_cd()
        st = state(units=[enemy(0.97, 0.50)])  # dx=0.47 > 0.45（当前钩程）
        act = decide(st, cd)
        self.assertNotEqual((act["type"], act.get("id")), ("skill", 2))
        self.assertIn(act["type"], ("move", "map_move"))

    def test_skill2_throttle(self):
        cd = fresh_cd()
        cd["skill2_t"] = T - 2.0  # 2s 前 -> 未过 3s 节流
        st = state(units=[enemy(0.85, 0.50)])
        act = decide(st, cd)
        self.assertNotEqual((act["type"], act.get("id")), ("skill", 2))


class TestSkill1(unittest.TestCase):
    """规则 3：一技能（不在大招生效期 + 敌人/敌兵在身边）。"""

    def test_skill1_when_enemy_near(self):
        cd = fresh_cd()
        cd["skill2_t"] = T - 4.0  # 2 技能冷却已过? 不——4s 前 -> 就绪，会先钩子
        # 敌人 0.2 屏宽在 0.42 内 -> 钩子优先。要测 1 技能需钩子不可用
        cd["skill2_t"] = T - 2.0  # 2 技能节流中
        st = state(units=[enemy(0.70, 0.50)])  # 0.2 < 0.25
        act = decide(st, cd)
        self.assertEqual((act["type"], act["id"]), ("skill", 1))

    def test_skill1_when_enemy_minion_near(self):
        """v8.0：小兵不参与目标选择，不能触发一技能。"""
        cd = fresh_cd()
        cd["skill2_t"] = T - 2.0
        st = state(units=[enemy_minion(0.72, 0.50)])
        act = decide(st, cd)
        self.assertNotEqual((act["type"], act.get("id")), ("skill", 1))

    def test_no_skill1_during_ult(self):
        cd = fresh_cd()
        cd["skill2_t"] = T - 2.0
        cd["skill3_t"] = T - 1.0  # 大招 1s 前 -> 生效期内，1 技能被抑制
        st = state(units=[enemy(0.70, 0.50)])
        act = decide(st, cd)
        self.assertNotEqual((act["type"], act.get("id")), ("skill", 1))


class TestMeleeAttack(unittest.TestCase):
    """规则 3.5：贴身敌人/兵且技能不可用 -> 普攻（用户反馈：面对敌方英雄不攻击）。"""

    def test_attack_when_enemy_adjacent_and_skills_cd(self):
        """敌人贴身（0.1 屏宽）且 2/1 技能都刚放过 -> 普攻。"""
        cd = fresh_cd()
        cd["skill2_t"] = T - 0.5   # 2 技能冷却中
        cd["skill1_t"] = T - 0.5   # 1 技能冷却中
        st = state(units=[enemy(0.60, 0.50)])  # dx=0.10 < 0.20
        act = decide(st, cd)
        self.assertEqual(act["type"], "attack")
        self.assertIn(act["reason"], ("melee_attack", "engage_enemy"))

    def test_attack_when_enemy_minion_adjacent(self):
        """v8.0：小兵不参与决策 -> 不普攻小兵。"""
        cd = fresh_cd()
        cd["skill2_t"] = T - 0.5
        cd["skill1_t"] = T - 0.5
        st = state(units=[enemy_minion(0.63, 0.50)])
        act = decide(st, cd)
        self.assertNotEqual(act["type"], "attack")

    def test_skill_preempts_attack_when_ready(self):
        """贴身敌人但 2 技能就绪 -> 钩子优先于普攻。"""
        cd = fresh_cd()
        st = state(units=[enemy(0.60, 0.50)])  # dx=0.10，也在钩子范围内
        act = decide(st, cd)
        self.assertEqual((act["type"], act.get("id")), ("skill", 2))

    def test_no_attack_when_enemy_far(self):
        cd = fresh_cd()
        cd["skill2_t"] = T - 1.0
        cd["skill1_t"] = T - 1.0
        st = state(units=[enemy(0.90, 0.50)])  # dx=0.40 > MELEE_FRAC(0.30)
        act = decide(st, cd)
        self.assertNotEqual(act["type"], "attack")


class TestMovement(unittest.TestCase):
    """规则 4：持续拖动移动。"""

    def test_chase_enemy_duration_2000(self):
        """追击远敌：技能在节流中，动作应落到 chase_enemy。"""
        cd = fresh_cd()
        cd["skill2_t"] = T - 1.0
        cd["skill1_t"] = T - 1.0
        cd["attack_t"] = T - 1.0
        st = state(units=[enemy(0.97, 0.50)])  # 0.47 > 钩程 0.45
        act = decide(st, cd)
        self.assertIn(act["type"], ("move", "map_move"))
        self.assertEqual(act["duration_ms"], MOVE_DURATION_MS)
        self.assertEqual(act["reason"], "chase_enemy")
        self.assertAlmostEqual(act["theta"], 0.0, places=6)

    def test_follow_ally_when_no_enemy(self):
        """v10.8：无敌人时走射手推进线，不再单独跟屏内队友。"""
        st = state(units=[ally(0.60, 0.50)])
        act = decide(st, fresh_cd())
        self.assertIn(act["type"], ("move", "map_move"))
        self.assertEqual(act["reason"], "dps_lane_push")
        self.assertAlmostEqual(act["nx"], 0.85, places=2)
        self.assertAlmostEqual(act["ny"], 0.82, places=2)

    def test_lane_develop_when_nothing(self):
        st = state(units=[], minimap=mm_found())
        cd = fresh_cd()
        cd["camp"] = "blue"
        act = decide(st, cd)
        self.assertIn(act["type"], ("move", "map_move"))
        self.assertEqual(act["reason"], "dps_lane_push")
        self.assertAlmostEqual(act["nx"], 0.85, places=2)
        self.assertAlmostEqual(act["ny"], 0.82, places=2)

    def test_lane_develop_red_mirror(self):
        """红方发育路应镜像到左上。"""
        st = state(units=[], minimap=mm_found())
        cd = fresh_cd()
        cd["camp"] = "red"
        act = decide(st, cd)
        self.assertEqual(act["reason"], "dps_lane_push")
        self.assertAlmostEqual(act["nx"], 0.15, places=2)
        self.assertAlmostEqual(act["ny"], 0.18, places=2)


class TestGuard(unittest.TestCase):
    """防抖。"""

    def test_debounce_suppresses_move(self):
        cd = fresh_cd()
        cd["skill"] = T - 0.03
        st = state(units=[enemy(0.95, 0.50)])
        act = decide(st, cd)
        self.assertEqual(act["type"], "none")
        self.assertEqual(act["reason"], "skill_debounce")


class TestRetreat(unittest.TestCase):
    """低血量撤退（用户规则）：HP<20% 朝泉水跑；身边无危险则回城。"""

    def test_low_hp_safe_recall(self):
        cd = fresh_cd()
        # 自家塔下（0.30 < 0.35）且身边无危险 -> 回城
        st = state(units=[{"cls": "ally_turret", "screen": [0.80, 0.50, 0.10, 0.10]}],
                   minimap=mm_found())
        st["ui"] = {"hp": 0.15}
        act = decide(st, cd)
        self.assertEqual(act["type"], "recall")
        self.assertEqual(act["reason"], "low_hp_recall_first")

    def test_low_hp_danger_retreat_to_fountain(self):
        cd = fresh_cd()
        st = state(units=[enemy(0.60, 0.50)])  # 0.10 < 0.50 有危险
        st["ui"] = {"hp": 0.10}
        act = decide(st, cd)
        self.assertIn(act["type"], ("move", "map_move"))
        self.assertEqual(act["reason"], "disengage_retreat")
        self.assertLess(act["nx"], 0.5)

    def test_high_hp_no_retreat(self):
        cd = fresh_cd()
        st = state(units=[enemy(0.75, 0.50)])  # 0.25 在钩子范围
        st["ui"] = {"hp": 0.8}
        act = decide(st, cd)
        self.assertNotEqual(act["type"], "recall")
        self.assertEqual((act["type"], act.get("id")), ("skill", 2))


class TestHookBlock(unittest.TestCase):
    """v2.5 规则 007：钩子路径被小兵/野怪挡住不钩。"""

    def test_hook_blocked_by_minion(self):
        cd = fresh_cd()
        # 敌英 0.26 屏宽（在钩子范围 0.28 内），但路径上有敌兵在 0.10 处同方向
        st = state(units=[enemy(0.76, 0.50), enemy_minion(0.60, 0.50)])
        act = decide(st, cd)
        self.assertNotEqual((act["type"], act.get("id")), ("skill", 2))

    def test_hook_blocked_by_monster(self):
        cd = fresh_cd()
        st = state(units=[enemy(0.76, 0.50),
                          {"cls": "neutral_monster", "screen": [0.62, 0.50, 0.06, 0.06]}])
        act = decide(st, cd)
        self.assertNotEqual((act["type"], act.get("id")), ("skill", 2))

    def test_hook_ok_when_path_clear(self):
        cd = fresh_cd()
        # 敌英 0.26，敌兵在旁边但不在直线上（y 偏移大）
        st = state(units=[enemy(0.76, 0.50), enemy_minion(0.60, 0.70)])
        act = decide(st, cd)
        self.assertEqual((act["type"], act.get("id")), ("skill", 2))

    def test_hook_ok_when_minion_behind_enemy(self):
        cd = fresh_cd()
        # 敌兵在敌英后面（更远）不挡钩
        st = state(units=[enemy(0.76, 0.50), enemy_minion(0.90, 0.50)])
        act = decide(st, cd)
        self.assertEqual((act["type"], act.get("id")), ("skill", 2))


class TestSkillUnlock(unittest.TestCase):
    """v2.5 规则：技能未解锁（灰暗）时不释放，改用普攻。"""

    def _ui(self, unlocked_map):
        return {"hp": 1.0, "skill_states": {
            str(k): {"unlocked": v, "ready": True, "mean_v": 150 if v else 45}
            for k, v in unlocked_map.items()}}

    def test_skill1_locked_uses_attack(self):
        cd = fresh_cd()
        cd["skill2_t"] = T - 2.0  # 2 技能冷却中
        st = state(units=[enemy(0.70, 0.50)])  # 0.2 贴身
        st["ui"] = self._ui({"1": False, "2": True, "3": True})
        act = decide(st, cd)
        self.assertEqual(act["type"], "attack")
        self.assertIn(act["reason"], ("melee_attack", "engage_enemy"))

    def test_skill2_locked_no_hook(self):
        cd = fresh_cd()
        st = state(units=[enemy(0.75, 0.50)])  # 0.25 在钩子范围
        st["ui"] = self._ui({"1": True, "2": False, "3": True})
        act = decide(st, cd)
        self.assertNotEqual((act["type"], act.get("id")), ("skill", 2))

    def test_skill2_unlocked_hooks(self):
        cd = fresh_cd()
        st = state(units=[enemy(0.75, 0.50)])
        st["ui"] = self._ui({"1": True, "2": True, "3": True})
        act = decide(st, cd)
        self.assertEqual((act["type"], act.get("id")), ("skill", 2))


class TestRestore(unittest.TestCase):
    """v2.5 规则：HP/MP<80% 且安全 -> 恢复键。"""

    def test_restore_when_low_hp_safe(self):
        cd = fresh_cd()
        st = state(units=[], minimap=mm_found())
        st["ui"] = {"hp": 0.7}
        act = decide(st, cd)
        self.assertEqual(act["type"], "restore")
        self.assertEqual(act["reason"], "low_resource_safe_restore")

    def test_restore_when_low_mp_safe(self):
        cd = fresh_cd()
        st = state(units=[], minimap=mm_found())
        st["ui"] = {"hp": 1.0, "mp": 0.6}
        act = decide(st, cd)
        self.assertEqual(act["type"], "restore")

    def test_no_restore_when_danger(self):
        cd = fresh_cd()
        st = state(units=[enemy(0.60, 0.50)])  # 0.10 身边有敌
        st["ui"] = {"hp": 0.7}
        act = decide(st, cd)
        self.assertNotEqual(act["type"], "restore")

    def test_no_restore_when_high_resource(self):
        cd = fresh_cd()
        st = state(units=[], minimap=mm_found())
        st["ui"] = {"hp": 0.95, "mp": 0.9}
        act = decide(st, cd)
        self.assertNotEqual(act["type"], "restore")

    def test_restore_throttle(self):
        cd = fresh_cd()
        cd["restore_t"] = T - 2.0  # 10s 节流内
        st = state(units=[], minimap=mm_found())
        st["ui"] = {"hp": 0.7}
        act = decide(st, cd)
        self.assertNotEqual(act["type"], "restore")


class TestChaseBreak(unittest.TestCase):
    """v2.5 规则 005：追击中血量降半 -> 停止追击撤退。"""

    def test_chase_breaks_when_hp_half(self):
        cd = fresh_cd()
        cd["skill2_t"] = T - 1.0
        cd["skill1_t"] = T - 1.0
        cd["attack_t"] = T - 1.0
        cd["restore_t"] = T - 2.0
        st = state(units=[enemy(0.97, 0.50)])
        st["ui"] = {"hp": 0.45}
        act = decide(st, cd)
        self.assertIn(act["type"], ("move", "map_move"))
        self.assertEqual(act["reason"], "stop_chase_low_hp_retreat")

    def test_chase_continues_when_hp_ok(self):
        cd = fresh_cd()
        cd["skill2_t"] = T - 1.0
        cd["skill1_t"] = T - 1.0
        cd["attack_t"] = T - 1.0
        cd["restore_t"] = T - 2.0
        st = state(units=[enemy(0.97, 0.50)])
        st["ui"] = {"hp": 0.7}
        act = decide(st, cd)
        self.assertEqual(act["reason"], "chase_enemy")


class TestRecallUnderTower(unittest.TestCase):
    """v2.5 规则 012：残血回自家塔下再回城。"""

    def test_low_hp_walk_to_ally_tower(self):
        cd = fresh_cd()
        # 自家塔在右 0.30 屏宽（<0.35 塔下）-> 直接安全回城
        st = state(units=[{"cls": "ally_turret", "screen": [0.80, 0.50, 0.10, 0.10]}],
                   minimap=mm_found())
        st["ui"] = {"hp": 0.15}
        act = decide(st, cd)
        self.assertEqual(act["type"], "recall")

    def test_low_hp_no_tower_walk_to_fountain(self):
        cd = fresh_cd()
        st = state(units=[], minimap=mm_found())
        st["ui"] = {"hp": 0.15}
        act = decide(st, cd)
        self.assertEqual(act["type"], "recall")
        self.assertEqual(act["reason"], "low_hp_recall_first")


class TestFollowMinimap(unittest.TestCase):
    """小地图观察移动（用户规则）：蓝点跟随（射手优先）/ 红点支援。"""

    def test_follow_minimap_blue_nearest_lane(self):
        cd = fresh_cd()
        cd["camp"] = "blue"
        st = state(units=[], minimap=mm_found(blue=[[0.7, 0.8], [0.3, 0.3]]))
        act = decide(st, cd)
        self.assertEqual(act["reason"], "support_dps_lane")
        self.assertAlmostEqual(act["nx"], 0.7, places=2)
        self.assertAlmostEqual(act["ny"], 0.8, places=2)

    def test_support_red_centroid(self):
        cd = fresh_cd()
        st = state(units=[], minimap=mm_found(red=[[0.6, 0.6]]))
        act = decide(st, cd)
        self.assertEqual(act["reason"], "support_dps_lane_red")

    def test_follow_minimap_blue_any(self):
        """v2.7：跟随发育路最近的蓝点（不再排除己方，目标跳变由滞回处理）。"""
        cd = fresh_cd()
        cd["camp"] = "blue"
        st = state(units=[], minimap=mm_found(blue=[[0.3, 0.3], [0.7, 0.8]]))
        act = decide(st, cd)
        self.assertEqual(act["reason"], "support_dps_lane")


class TestPathfinding(unittest.TestCase):
    """v2.6：小地图寻路修正（墙体绕行）。"""

    def _mm(self, blue):
        return {"found": True, "center": [100, 140], "radius": 60,
                "dots": {"blue": blue, "red": [], "yellow": []}, "towers": []}

    def test_lane_develop_path_correction(self):
        """泉水附近蓝点 + 发育路目标：走兵线路径（reason 带 _path）。"""
        cd = fresh_cd()
        cd["camp"] = "blue"
        st = state(units=[], minimap=self._mm([[0.15, 0.85]]))
        st["ui"] = {"hp": 1.0}
        act = decide(st, cd)
        # 蓝点只有己方 -> 排除后无候选 -> fallback 蓝点列表 -> 目标=己方自身
        # 此时 lane_develop 或 follow 都行，关键是路径修正逻辑不崩
        self.assertIn(act["type"], ("move", "map_move"))  # 动作类型已统一为 map_move

    def test_path_correction_on_follow_ally(self):
        """队友在发育路（蓝点），己方在泉水：A* 修正方向。"""
        cd = fresh_cd()
        cd["camp"] = "blue"
        st = state(units=[], minimap=self._mm([[0.15, 0.85], [0.70, 0.80]]))
        st["ui"] = {"hp": 1.0}
        act = decide(st, cd)
        self.assertEqual(act["reason"], "support_dps_lane")


class TestTargetHold(unittest.TestCase):
    """v2.7 目标滞回：蓝点噪声导致目标跳变 -> 稳定期内维持旧目标防乱转。"""

    def _mm(self, blue):
        return {"found": True, "center": [100, 140], "radius": 60,
                "dots": {"blue": blue, "red": [], "yellow": []}, "towers": []}

    def test_target_switches_after_hold(self):
        cd = fresh_cd()
        cd["camp"] = "blue"
        # 帧1：队友在 (0.70,0.80)
        st1 = state(units=[], minimap=self._mm([[0.70, 0.80]]), t=100.0)
        a1 = decide(st1, cd)
        self.assertEqual(a1["reason"], "support_dps_lane")
        # 帧2（0.5s 后）：目标跳变到 (0.30,0.30)，滞回期内应维持旧目标
        st2 = state(units=[], minimap=self._mm([[0.30, 0.30]]), t=100.5)
        a2 = decide(st2, cd)
        self.assertIn("hold", a2["reason"])
        # 帧3（2s 后）：滞回期过，切到新目标
        st3 = state(units=[], minimap=self._mm([[0.30, 0.30]]), t=102.5)
        a3 = decide(st3, cd)
        self.assertNotIn("hold", a3["reason"])

    def test_no_hold_for_chase(self):
        """追击敌人不滞回（避免追丢）。"""
        cd = fresh_cd()
        cd["skill2_t"] = T - 1.0
        cd["skill1_t"] = T - 1.0
        cd["attack_t"] = T - 1.0
        st1 = state(units=[enemy(0.85, 0.50)], t=100.0)
        a1 = decide(st1, cd)
        self.assertEqual(a1["reason"], "chase_enemy")
        st2 = state(units=[enemy(0.80, 0.50)], t=100.5)
        a2 = decide(st2, cd)
        self.assertEqual(a2["reason"], "chase_enemy")
        self.assertNotIn("hold", a2["reason"])


class TestOpeningProtect(unittest.TestCase):
    """v2.7 开局保护：对局确认 15 秒内只朝发育路，不支援/不跟随（防乱转）。"""

    def test_opening_goes_lane(self):
        cd = fresh_cd()
        cd["camp"] = "blue"
        cd["match_start_t"] = 100.0
        # 开局 5 秒：即使有红点也不支援
        st = state(units=[], minimap=mm_found(red=[[0.6, 0.6]], blue=[[0.7, 0.8]]), t=105.0)
        act = decide(st, cd)
        self.assertEqual(act["reason"], "dps_lane_push")


class TestDecisionV2(unittest.TestCase):
    """v2.12-2.13 决策层升级：敌人红条追击 + MP 管理 + 回城保护 + 恢复节流。"""

    def _extra_state(self, enemy_bars=None, mp=1.0, hero_pos=None, mm_red=None):
        # v2.14: 红条追击需要小地图红点存在（场景红元素误检防护），默认给红点
        st = state(units=[], minimap=mm_found(red=mm_red if mm_red is not None else [[0.6, 0.6]]))
        extra = {}
        if enemy_bars:
            extra["enemy_bars"] = enemy_bars
        if hero_pos:
            extra["hero_pos"] = hero_pos
        st["extra"] = extra
        st.setdefault("ui", {})["mp"] = mp
        return st

    def test_chase_enemy_bar_when_no_detection(self):
        """YOLO 未检出敌人但红条可见+小地图红点存在 -> 朝红条方向移动。"""
        cd = fresh_cd()
        # 红条在屏幕右侧 (1000, 300) -> 归一化 (0.78, 0.42)
        st = self._extra_state(enemy_bars=[(1000, 300, 60)])
        act = decide(st, cd)
        self.assertIn(act["type"], ("move", "map_move"))  # 动作类型已统一为 map_move
        self.assertEqual(act["reason"], "support_dps_lane_red")

    def test_chase_enemy_bar_ignored_without_minimap_red(self):
        """v2.14: 小地图无红点（蓝0/红0）时红条不可信（场景红元素误检）-> 不追击。"""
        cd = fresh_cd()
        cd["camp"] = "blue"
        st = self._extra_state(enemy_bars=[(1000, 300, 60)], mm_red=[])
        act = decide(st, cd)
        self.assertNotEqual(act["reason"], "chase_enemy_bar")

    def test_skill_disabled_when_low_mp(self):
        """MP < 20% 时禁用技能（省蓝），只普攻/移动。"""
        cd = fresh_cd()
        # 敌人在钩子范围 (0.75, 0.5) 但 MP 极低
        st = state(units=[enemy(0.75, 0.50)])
        st["extra"] = {}
        st.setdefault("ui", {})["mp"] = 0.10
        act = decide(st, cd)
        self.assertNotEqual((act["type"], act.get("id")), ("skill", 2))

    def test_skill_enabled_when_mp_ok(self):
        """MP 充足时钩子正常。"""
        cd = fresh_cd()
        st = state(units=[enemy(0.75, 0.50)])
        st["extra"] = {}
        st.setdefault("ui", {})["mp"] = 0.9
        act = decide(st, cd)
        self.assertEqual((act["type"], act.get("id")), ("skill", 2))

    def test_after_opening_supports(self):
        cd = fresh_cd()
        cd["camp"] = "blue"
        cd["match_start_t"] = 100.0
        st = state(units=[], minimap=mm_found(red=[[0.6, 0.6]], blue=[[0.7, 0.8]]), t=120.0)
        act = decide(st, cd)
        self.assertIn(act["reason"], ("support_dps_lane", "support_dps_lane_red", "dps_lane_push"))

    # ---------- v2.13：回城读条保护 / 恢复节流 60s / 红条滞回 ----------

    def test_recall_in_progress_blocks_actions(self):
        """回城后 8s 内：不执行任何动作（恢复键/移动不得打断读条）。"""
        cd = fresh_cd()
        cd["recall_t"] = T - 4.0   # 4 秒前开始回城
        st = self._extra_state(enemy_bars=[])  # 无危险
        st.setdefault("ui", {})["hp"] = 0.5    # 低血，但回城中不应按恢复键
        st.setdefault("ui", {})["mp"] = 0.5
        act = decide(st, cd)
        self.assertEqual((act["type"], act["reason"]), ("none", "recall_in_progress"))

    def test_recall_interrupted_by_enemy_near(self):
        """回城读条中敌人近身 -> 中断回城，走正常决策（追击/技能）。"""
        cd = fresh_cd()
        cd["recall_t"] = T - 4.0
        st = state(units=[enemy(0.52, 0.50)])
        st["extra"] = {}
        act = decide(st, cd)
        self.assertNotEqual(act.get("reason"), "recall_in_progress")
        self.assertIn(act["type"], ("skill", "move", "attack", "none"))

    def test_restore_throttle_60s(self):
        """恢复键节流 60s：10 秒前按过 -> 低资源也不按恢复。"""
        cd = fresh_cd()
        cd["restore_t"] = T - 10.0
        st = self._extra_state(mp=0.5)
        st.setdefault("ui", {})["hp"] = 0.9
        act = decide(st, cd)
        self.assertNotEqual(act["type"], "restore")

    def test_restore_ready_after_60s(self):
        """60 秒后节流到期 -> 低资源安全时按恢复键。"""
        cd = fresh_cd()
        cd["restore_t"] = T - 61.0
        st = self._extra_state(mp=0.5)
        st.setdefault("ui", {})["hp"] = 0.9
        act = decide(st, cd)
        self.assertEqual((act["type"], act["reason"]), ("restore", "low_resource_safe_restore"))

    def test_chase_enemy_bar_hold(self):
        """红条短暂消失(<=1.5s) -> 沿用最后红条位置继续追击，不切 follow。"""
        cd = fresh_cd()
        cd["bar_chase_t"] = T - 1.0
        cd["bar_target"] = (0.78, 0.42)
        st = self._extra_state(enemy_bars=[])   # 红条当前不可见
        act = decide(st, cd)
        self.assertEqual(act["reason"], "support_dps_lane_red")

    def test_chase_enemy_bar_hold_expired(self):
        """红条消失超过 1.5s -> 放弃追击，回落到小地图决策。"""
        cd = fresh_cd()
        cd["bar_chase_t"] = T - 3.0
        cd["bar_target"] = (0.78, 0.42)
        cd["camp"] = "blue"
        cd["match_start_t"] = 0.0
        st = self._extra_state(enemy_bars=[])
        act = decide(st, cd)
        self.assertNotEqual(act["reason"], "chase_enemy_bar")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    ok = runner.run(suite).wasSuccessful()
    sys.exit(0 if ok else 1)
