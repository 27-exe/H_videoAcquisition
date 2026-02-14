import logging,json,os,emoji,asyncio,shutil,uuid
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

async def run_command(cmd):
    """通用异步执行命令行函数"""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


async def generate_single_thumbnail_async(t_video_path: str, cover_path: str):
    """
    单张12s处封面生成
    """
    # 使用异步子进程
    cmd = [
        'ffmpeg', '-ss', '12', '-i', t_video_path,
        '-frames:v', '1', '-q:v', '2', '-y', cover_path
    ]

    returncode, _, stderr = await run_command(cmd)

    if returncode == 0 and os.path.exists(cover_path):
        logger.debug(f"异步生成封面成功: {cover_path}")
        return cover_path
    else:
        logger.error(f"FFmpeg 失败: {stderr}")
        return None


async def get_video_info_async(video_path: str):
    """异步获取视频信息"""
    cmd = [
        'ffprobe', '-v', 'quiet', '-print_format', 'json',
        '-show_format', '-show_streams', video_path
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()

    data = json.loads(stdout.decode('utf-8'))
    duration = float(data['format']['duration'])
    # 转换为X min Y s格式
    minutes = int(duration // 60)
    seconds = int(duration % 60)
    duration_str = f"{minutes}min{seconds}s"
    width, height = 1920, 1080  # 默认值
    for stream in data['streams']:
        if stream['codec_type'] == 'video':
            width = int(stream.get('width', width))
            height = int(stream.get('height', height))
            break
    return duration, width, height ,duration_str


async def extract_frame_async(video_path, timestamp, output_path):
    """异步抽取单帧"""
    cmd = [
        'ffmpeg', '-ss', str(timestamp), '-i', video_path,
        '-frames:v', '1', '-q:v', '2', '-y', output_path
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await proc.wait()
    return os.path.exists(output_path)


def _draw_logic_sync(frames, thumb_path, is_vertical, info_dict):
    """
    纯 CPU 密集的绘图逻辑，保留同步写法以便在线程池运行
    """
    num = info_dict["num"]
    today = info_dict["today"]
    clean_name = info_dict["name"]
    duration_str = info_dict["str"]
    # 获取单张图宽高
    with Image.open(frames[0]) as im:
        w, h = im.size

    if is_vertical:
        # 竖屏逻辑：1080x1920画布，四宫格缩放至864x1536
        canvas_w, canvas_h = 1080, 1920
        target_w, target_h = 864, 1536  # 四宫格区域
        white_h = 384  # 底部白块高度（1920 - 1536）
        left_margin = (canvas_w - target_w) // 2  # 左侧留白

        # 计算缩放比例
        scale_w = target_w / (w * 2)  # 2列
        scale_h = target_h / (h * 2)  # 2行
        scale = min(scale_w, scale_h)
        new_w = int(w * scale)
        new_h = int(h * scale)

        # 创建画布
        canvas = Image.new('RGB', (canvas_w, canvas_h), (255, 255, 255))

        # 四宫格坐标（居中）
        positions = [
            (left_margin, 0), (left_margin + new_w, 0),
            (left_margin, new_h), (left_margin + new_w, new_h)
        ]

        # 粘贴帧
        for (x, y), fp in zip(positions, frames):
            with Image.open(fp) as im:
                im_resized = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
                canvas.paste(im_resized, (x, y))
    else:
        # 横屏逻辑：1920x1080画布，六宫格
        scale_w = 1920 / (w * 3)
        scale_h = 1080 / (h * 3)
        scale = min(scale_w, scale_h)
        new_w = int(w * scale)
        new_h = int(h * scale)
        white_h = 360  # 下部白块高度

        # 创建画布
        canvas_w, canvas_h = 1920, 1080
        canvas = Image.new('RGB', (canvas_w, canvas_h), (255, 255, 255))

        # 宫格坐标
        positions = [
            (0, 0), (new_w, 0), (new_w * 2, 0),
            (0, new_h), (new_w, new_h), (new_w * 2, new_h),(new_w,new_h*2)
        ]

        # 粘贴帧
        for (x, y), fp in zip(positions, frames):
            with Image.open(fp) as im:
                im_resized = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
                canvas.paste(im_resized, (x, y))

    # 绘制文字
    draw = ImageDraw.Draw(canvas)
    try:
        font_file = str(Path(__file__).resolve().parent.parent / "SimHei.ttf")
        top_font = ImageFont.truetype(font_file, size=200 if not is_vertical else 130)
        num_font = ImageFont.truetype(font_file, size=160 if not is_vertical else 100)
        small_font = ImageFont.truetype(font_file, size=45 if not is_vertical else 35)
        large_font = ImageFont.truetype(font_file, size=45 if not is_vertical else 45)
    except IOError:
        logger.error(f"字体文件未找到! 将回退到默认字体，可能无法显示中文。")
        top_font = ImageFont.load_default()
        num_font = ImageFont.load_default()
        small_font = ImageFont.load_default()
        large_font = ImageFont.load_default()

    # 文字内容
    top_text = "TOP"
    num_text = f".{num}"
    date_text = f"Date: {today}" if today else f"Date: 2025/9/9"
    duration_text = duration_str
    no_emoji_name = emoji.replace_emoji(clean_name, replace='')
    if is_vertical:
        clean_name_display = no_emoji_name[:30] + "..." if len(no_emoji_name) > 30  else no_emoji_name
    else:
        clean_name_display = no_emoji_name[:10] + "..." if len(no_emoji_name) > 10 else no_emoji_name

    if is_vertical:
        # 竖屏文字布局
        white_area_y_start = canvas_h - white_h
        left_margin = 20
        right_margin = 20
        right_align_x = canvas_w - right_margin

        # 绘制左侧文字 ("TOP.num")
        top_y = white_area_y_start + 180
        draw.text((left_margin, top_y), top_text, font=top_font, fill="black", anchor="ls")
        top_bbox = draw.textbbox((left_margin+30, top_y), top_text, font=top_font, anchor="ls")
        num_x = top_bbox[2]
        draw.text((num_x, top_y), num_text, font=num_font, fill="black", anchor="ls")

        # 绘制右侧文字 (日期, 时长, 标题)
        date_y = white_area_y_start + 60
        draw.text((right_align_x, date_y), date_text, font=small_font, fill="black", anchor="ra")
        duration_y = date_y + 60
        draw.text((right_align_x, duration_y), duration_text, font=small_font, fill="black", anchor="ra")
        title_y = duration_y + 80
        draw.text((right_align_x, title_y), clean_name_display, font=large_font, fill="black", anchor="ra")
    else:
        # 横屏文字布局
        white_area_y_start = canvas_h - white_h
        left_margin = 40
        right_margin = 80
        right_align_x = canvas_w - right_margin

        # 绘制左侧文字 ("TOP.num")
        top_y = white_area_y_start + 240
        draw.text((left_margin, top_y), top_text, font=top_font, fill="black", anchor="ls")
        top_bbox = draw.textbbox((left_margin, top_y), top_text, font=top_font, anchor="ls")
        num_x = top_bbox[2]
        draw.text((num_x, top_y), num_text, font=num_font, fill="black", anchor="ls")

        # 绘制右侧文字 (日期, 时长, 标题)
        date_y = white_area_y_start + 40
        draw.text((right_align_x, date_y), date_text, font=small_font, fill="black", anchor="ra")
        duration_y = date_y + 60
        draw.text((right_align_x, duration_y), duration_text, font=small_font, fill="black", anchor="ra")
        title_y = duration_y + 80
        draw.text((right_align_x, title_y), clean_name_display, font=large_font, fill="black", anchor="ra")

    canvas.save(thumb_path, 'JPEG', quality=85)
    return thumb_path


def stitch_cover(count, thumb_path, cover_path):
    try:
        thumb_img = Image.open(thumb_path)
        cover_img = Image.open(cover_path)
        if count == 7:
            # 上下拼接
            canvas = Image.new('RGB', (1920, 2160), (255, 255, 255))  # 调整宽度
            positions = [(0, 0), (0, 1080)]

            images = [thumb_img, cover_img]
            for (x, y), im in zip(positions, images):
                canvas.paste(im, (x, y))

            canvas.save(thumb_path, 'JPEG', quality=85)
        elif count == 4:#左右拼接
            canvas = Image.new('RGB', (2160, 1920), (255, 255, 255))  # 调整宽度
            positions = [(0, 0), (1080,0)]

            images = [thumb_img, cover_img]
            for (x, y), im in zip(positions, images):
                canvas.paste(im, (x, y))
    except Exception as e:
        logger.error(f"stitch_cover 拼接失败: {e}")
    finally:
        # 关闭图像，释放内存
        try:
            thumb_img.close()
            cover_img.close()
        except:
            pass


async def generate_thumbnail(t_video_path: str, thumb_path: str,cover_path, vid_id,num, today, clean_name):
    if t_video_path == 0:
        return False
    # 1. 创建唯一的临时工作目录，彻底避免并发冲突
    job_id = uuid.uuid4().hex
    temp_dir = Path("temp") / f"thumb_{job_id}"
    temp_dir.mkdir(exist_ok=True)
    thumb_id = os.path.join(thumb_path, f"{vid_id}.jpg")
    cover_id = os.path.join(cover_path, f"{vid_id}.jpg")
    os.makedirs(thumb_path, exist_ok=True)
    os.makedirs(cover_path, exist_ok=True)

    try:
        # 2. 异步获取视频信息
        duration, width, height , duration_str= await get_video_info_async(t_video_path)
        is_vertical = width < height

        # 3. 计算抽帧时间点
        count = 4 if is_vertical else 7
        times = [duration * i / (count + 1) for i in range(1, count + 1)]

        # 4. 并发抽帧 (利用 asyncio.gather 提升效率)
        frame_tasks = []
        frame_paths = []
        for idx, t in enumerate(times):
            f_path = str(temp_dir / f"{idx}.jpg")
            frame_paths.append(f_path)
            frame_tasks.append(extract_frame_async(t_video_path, t, f_path))

        await asyncio.gather(*frame_tasks)

        # 5. 检查帧是否完整
        valid_frames = [f for f in frame_paths if os.path.exists(f)]
        if len(valid_frames) < count:
            return None
        # 6. 抽取单张封面
        await generate_single_thumbnail_async(t_video_path,cover_id)
        # 7. 将图片合成任务丢入线程池，避免阻塞主事件循环
        loop = asyncio.get_running_loop()
        info_dict = {"num": num, "today": today, "name": clean_name,"str":duration_str}

        await loop.run_in_executor(
            None, _draw_logic_sync, valid_frames, thumb_id, is_vertical, info_dict
        )
        await loop.run_in_executor(
            None,stitch_cover,count,thumb_id,cover_id
        )
        return True

    except Exception as e:
        logger.error(f"生成预览图失败: {e}")
        return False
    finally:
        # 7. 清理临时文件夹
        if temp_dir.exists():
            shutil.rmtree(temp_dir)

