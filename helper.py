import json
import pathlib
import time
from typing import Any, Dict, List, Literal, Tuple

import fire
from rich import box, console, table
from tencentcloud.clb.v20180317.clb_client import ClbClient
from tencentcloud.clb.v20180317.models import (
    BatchModifyTargetWeightRequest,
    DescribeLoadBalancersDetailRequest,
    DescribeTargetsRequest,
    RsWeightRule,
    Target,
)
from tencentcloud.common.credential import Credential
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile

XDG_DIR_CONFIG_FILE = pathlib.Path.home() / ".config" / "tc-clb-helper.json"
CURRENT_DIR_CONFIG_FILE = pathlib.Path(__file__).parent / "tc-clb-helper.json"


def read_config():
    """按优先级读取配置文件，优先级从高到低为：

    - 当前目录的 tc-clb-helper.json
    - XDG 目录下的 tc-clb-helper.json
    """
    if CURRENT_DIR_CONFIG_FILE.is_file():
        config_file = CURRENT_DIR_CONFIG_FILE
    elif XDG_DIR_CONFIG_FILE.is_file():
        config_file = XDG_DIR_CONFIG_FILE
    else:
        raise Exception(
            f"配置文件 {CURRENT_DIR_CONFIG_FILE!s} 和 {XDG_DIR_CONFIG_FILE!s} 都不存在"
        )

    try:
        with open(config_file, "r") as fp:
            content = json.load(fp=fp)
    except json.JSONDecodeError:
        raise Exception("配置文件 {config_file!s} 不是合法的 JSON")

    return content


class TencentCloudCLBHelper:
    def __init__(
        self,
        secret_id: str,
        secret_key: str,
        region="ap-shanghai",
        endpoint="clb.tencentcloudapi.com",
    ) -> None:
        self._console = console.Console()
        self._client = ClbClient(
            credential=Credential(secret_id=secret_id, secret_key=secret_key),
            region=region,
            profile=ClientProfile(httpProfile=HttpProfile(endpoint=endpoint)),
        )

    def _render_table(
        self,
        table_title: str,
        table_columns: Tuple,
        table_data: List[Dict[str, Any]],
    ):
        """表格渲染打印"""
        # 初始化
        data_table = table.Table(
            title=table_title,
            show_header=True,
            header_style="bold",
            box=box.MINIMAL,
            row_styles=["dim", ""],
        )
        # 设置 columns
        for column in table_columns:
            data_table.add_column(column)
        # 设置数据行
        for row_data in table_data:
            data_table.add_row(*[str(row_data.get(column)) for column in table_columns])
        # print
        self._console.print(data_table)

    def _req_describe_load_balancers_detail(self) -> List[Dict[str, Any]]:
        """SDK 请求 DescribeLoadBalancersDetail"""
        req = DescribeLoadBalancersDetailRequest()
        req.Limit = 100

        resp = self._client.DescribeLoadBalancersDetail(req)
        if resp.LoadBalancerDetailSet is None:
            raise Exception("DescribeLoadBalancersDetail 失败")

        return [
            {
                "LoadBalancerId": lb.LoadBalancerId,
                "LoadBalancerName": lb.LoadBalancerName,
                "Status": lb.Status,
                "Address": lb.Address,
            }
            for lb in resp.LoadBalancerDetailSet
        ]

    def list_clbs(self):
        """展示 CLB 列表"""

        table_data = self._req_describe_load_balancers_detail()
        self._render_table(
            table_title="CLB 列表",
            table_columns=("LoadBalancerId", "Address", "Status", "LoadBalancerName"),
            table_data=table_data,
        )

    def _req_describe_targets(self, clb_id: str):
        """SDK 请求 DescribeTargets"""
        # DescribeTargets
        req = DescribeTargetsRequest()
        req.LoadBalancerId = clb_id
        resp = self._client.DescribeTargets(req)
        if resp.Listeners is None:
            raise Exception("DescribeTargets 失败，Listeners 为 None")
        if len(resp.Listeners) != 1:
            raise Exception(f"DescribeTargets 失败，监听器数量为 {len(resp.Listeners)} != 1")

        listener = resp.Listeners[0]
        if listener.Rules is None:
            raise Exception("DescribeTargets 失败，Rules 为 None")
        if len(listener.Rules) != 1:
            raise Exception(f"DescribeTargets 失败，Rules 数量为 {len(resp.Listeners)} != 1")

        # 按 PrivateIpAddresses[0] 聚合，不用 InstanceName 防止有 instance 重名的情况
        targets_by_ip = {}
        for target in listener.Rules[0].Targets:
            private_ip = target.PrivateIpAddresses[0]
            if private_ip not in targets_by_ip:
                targets_by_ip[private_ip] = {
                    "InstanceId": target.InstanceId,
                    "InstanceName": target.InstanceName,
                    "PrivateIpAddresses": private_ip,
                    "Ports": [],
                }
            targets_by_ip[private_ip]["Ports"].append((target.Port, target.Weight))

        # 按 InstanceName 排序后返回
        target_instances = sorted(
            targets_by_ip.values(), key=lambda instance: instance["InstanceName"]
        )
        return list(target_instances), listener.ListenerId, listener.Rules[0].LocationId

    def list_clb_targets(self, clb_id: str):
        """展示 CLB 后端列表"""
        target_instances, _, _ = self._req_describe_targets(clb_id=clb_id)

        # 处理一下数据
        for i in target_instances:
            # 计算该 instance 下总 Ports 数量
            i["PortsAmount"] = len(i["Ports"])
            # 根据 Ports 的权重用不同颜色渲染，权重为 0 红色并加粗
            i["Port[Weight] List"] = " ".join(
                f"[bold red]{port[0]}[{port[1]}][/bold red]"
                if port[1] == 0
                else f"[green]{port[0]}[{port[1]}][/green]"
                for port in i["Ports"]
            )

        self._render_table(
            table_title=f"CLB 「{clb_id}」 后端列表",
            table_columns=(
                "InstanceId",
                "InstanceName",
                "PrivateIpAddresses",
                "PortsAmount",
                "Port[Weight] List",
            ),
            table_data=target_instances,
        )

    def _req_batch_modify_target_weight(
        self,
        clb_id: str,
        listener_id: str,
        location_id: str,
        instance_id: str,
        ports: List[int],
        weight: Literal[0, 10],
    ):
        """SDK 请求 BatchModifyTargetWeight"""
        target_list = []
        for port in ports:
            target = Target()
            target.InstanceId = instance_id
            target.Port = port
            target_list.append(target)

        rule = RsWeightRule()
        rule.ListenerId = listener_id
        rule.LocationId = location_id
        rule.Weight = weight
        rule.Targets = target_list

        req = BatchModifyTargetWeightRequest()
        req.LoadBalancerId = clb_id
        req.ModifyList = [rule]

        return self._client.BatchModifyTargetWeight(req)

    def _change_clb_instance_weight(
        self,
        clb_id: str,
        instance_id: str,
        weight: Literal[0, 10],
    ):
        """修改 CLB 后端中单个节点所有端口的权重"""
        target_instances, listener_id, location_id = self._req_describe_targets(
            clb_id=clb_id
        )

        # 如果是下线操作，检查其他节点必须有端口权重不为 0
        if weight == 0:
            others_offline = True
            for instance in target_instances:
                # 跳过当前节点
                if instance["InstanceId"] == instance_id:
                    continue
                for port in instance["Ports"]:
                    if port[1] != 0:
                        others_offline = False
                        break
                if not others_offline:
                    break

            if others_offline:
                raise Exception(f"CLB {clb_id} 除 {instance_id} 外的其他节点都为离线，禁止操作，否则服务挂掉！")

        # 检查要操作的节点在 CLB 后端中存在
        current_instance = None
        for instance in target_instances:
            if instance["InstanceId"] == instance_id:
                current_instance = instance
                break
        if not current_instance:
            raise Exception(f"CLB {clb_id} 的后端中没有节点 {instance_id} 的端口")

        resp = self._req_batch_modify_target_weight(
            clb_id=clb_id,
            listener_id=listener_id,
            location_id=location_id,
            instance_id=instance_id,
            ports=[port[0] for port in current_instance["Ports"]],
            weight=weight,
        )
        self._console.print("接口返回", resp)
        self._console.print("延时 3 秒等待生效...")

    def online_clb_instance(self, clb_id: str, instance_id: str):
        """上线 CLB 后端节点"""
        self.list_clb_targets(clb_id=clb_id)
        self._change_clb_instance_weight(
            clb_id=clb_id, instance_id=instance_id, weight=10
        )
        time.sleep(3)
        self.list_clb_targets(clb_id=clb_id)

    def offline_clb_instance(self, clb_id: str, instance_id: str):
        """下线 CLB 后端节点"""
        self.list_clb_targets(clb_id=clb_id)
        self._change_clb_instance_weight(
            clb_id=clb_id, instance_id=instance_id, weight=0
        )
        time.sleep(3)
        self.list_clb_targets(clb_id=clb_id)


if __name__ == "__main__":
    helper = TencentCloudCLBHelper(**read_config())
    fire.Fire(helper)
