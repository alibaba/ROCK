import pytest

from rock.utils import ImageUtil


@pytest.mark.asyncio
async def test_split():
    image_tuple = ImageUtil.split_image_name(
        "jefzda/sweap-images:gravitational.teleport-gravitational__teleport-82185f232ae8974258397e121b3bc2ed0c3729ed-v626ec2a48416b10a88641359a169d99e935ff03"
    )
    assert (
        "jefzda",
        "sweap-images",
        "gravitational.teleport-gravitational__teleport-82185f232ae8974258397e121b3bc2ed0c3729ed-v626ec2a48416b10a88641359a169d99e935ff03",
    ) == image_tuple

    image_tuple = ImageUtil.split_image_name("jyangballin/swesmith.x86_64.hips_1776_autograd.ac044f0d")
    assert ("jyangballin", "swesmith.x86_64.hips_1776_autograd.ac044f0d", "latest") == image_tuple

    image_tuple = ImageUtil.split_image_name("python")
    assert ("library", "python", "latest") == image_tuple

    image_tuple = ImageUtil.split_image_name("namespace/name_part_1/name_part_2")
    assert ("namespace", "name_part_1/name_part_2", "latest") == image_tuple

    with pytest.raises(Exception):
        ImageUtil.split_image_name("name:tag:redundant_tag")


@pytest.mark.asyncio
async def test_parse_registry_and_others():
    image_tuple = ImageUtil.parse_registry_and_others("namespace/name_part_1/name_part_2")
    assert ("", "namespace/name_part_1/name_part_2") == image_tuple

    image_tuple = ImageUtil.parse_registry_and_others("docker.io/namespace/name")
    assert ("docker.io", "namespace/name") == image_tuple

    image_tuple = ImageUtil.parse_registry_and_others("rex-registry-vpc.cn-hangzhou.cr.aliyuncs.com/namespace/name:tag")
    assert ("rex-registry-vpc.cn-hangzhou.cr.aliyuncs.com", "namespace/name:tag") == image_tuple
