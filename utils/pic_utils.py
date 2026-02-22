import logging,json,os,emoji,asyncio,shutil,uuid
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageOps,ImageFilter

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
    单张7s处封面生成
    """
    # 使用异步子进程
    cmd = [
        'ffmpeg', '-ss', '7', '-i', t_video_path,
        '-frames:v', '1', '-q:v', '2', '-y', cover_path
    ]

    returncode, _, stderr = await run_command(cmd)

    if returncode == 0 and os.path.exists(cover_path):
        logger.debug(f"生成封面成功: {cover_path}")
        return cover_path
    else:
        logger.error(f"FFmpeg 失败: {stderr}")
        return None


async def generate_mini_thumb_async(input_path: str, output_path: str):
    """
    使用 FFmpeg 生成 Telegram 要求的 320px 缩略图
    User for: InputMediaUploadedDocument(thumb=...)
    """
    if not os.path.exists(input_path):
        return False

    # scale逻辑: 如果宽>高(横屏),宽=320,高自适应; 否则(竖屏),高=320,宽自适应
    vf_scale = "scale='if(gt(iw,ih),320,-1)':'if(gt(iw,ih),-1,320)'"

    cmd = [
        'ffmpeg', '-y', '-i', input_path,
        '-vf', vf_scale,
        '-q:v', '1',  # 质量 1 (1-31, 1为最高质量，因为图很小所以可以用高质量)
        output_path
    ]

    returncode, _, stderr = await run_command(cmd)

    if returncode == 0 and os.path.exists(output_path):
        # logger.debug(f"生成320px缩略图成功: {output_path}")
        return True
    else:
        logger.error(f"生成缩略图失败: {stderr}")
        return False

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
    num = info_dict["num"]
    today = info_dict["today"]
    clean_name = info_dict["name"]
    duration_str = info_dict["str"]


    canvas_w, canvas_h = (1080, 1920) if is_vertical else (1920, 1080)
    white_h = 384 if is_vertical else 360  # 底部文字区高度
    grid_h = canvas_h - white_h  # 宫格实际可用高度

    # 1. 创建高斯模糊背景
    with Image.open(frames[0]) as bg_ref:
        # 先 fit 到目标尺寸
        bg = ImageOps.fit(bg_ref, (canvas_w, canvas_h), method=Image.Resampling.LANCZOS)

        # 缩小 -> 模糊 -> 放大
        small_bg = bg.resize((canvas_w // 10, canvas_h // 10), resample=Image.Resampling.NEAREST)
        small_bg = small_bg.filter(ImageFilter.GaussianBlur(radius=5))  # 缩小了10倍，半径也相应减小
        canvas = small_bg.resize((canvas_w, canvas_h), resample=Image.Resampling.BILINEAR)

        # 转换为 RGBA 以支持半透明绘制
        canvas = canvas.convert("RGBA")

    # 2. 计算宫格布局
    cols = 2 if is_vertical else 3
    rows = 2 if is_vertical else 2  # ←←← 横屏改成 2 行（原来是 3）

    cell_w = canvas_w // cols
    cell_h = grid_h // rows  # 横屏现在是 720//2 = 360（完美16:9）

    # 3. 逐个粘贴帧 (等比例缩放并居中)
    for idx, fp in enumerate(frames):
        with Image.open(fp) as im:
            orig_w, orig_h = im.size
            # 计算在单元格内的缩放比例
            scale = min(cell_w / orig_w, cell_h / orig_h)
            new_w, new_h = int(orig_w * scale), int(orig_h * scale)
            im_resized = im.resize((new_w, new_h), Image.Resampling.LANCZOS)

            # 计算在格子内的居中偏移量
            offset_x = (cell_w - new_w) // 2
            offset_y = (cell_h - new_h) // 2

            r, c = divmod(idx, cols)

            # 计算最终粘贴坐标
            paste_x = c * cell_w + offset_x
            paste_y = r * cell_h + offset_y

            canvas.paste(im_resized, (paste_x, paste_y))

    # 4. 绘制底部半透明文字遮罩
    # 创建一个透明的图层用于绘制遮罩
    overlay = Image.new('RGBA', canvas.size, (255, 255, 255, 0))
    draw_ov = ImageDraw.Draw(overlay)

    # 绘制半透明矩形 (200 为透明度，可调)
    text_bg_region = [0, grid_h, canvas_w, canvas_h]
    draw_ov.rectangle(text_bg_region, fill=(255, 255, 255, 200))

    # 合并图层
    canvas = Image.alpha_composite(canvas, overlay)


    # 绘制文字，尝试加载中文字体，失败则回退到默认字体（可能无法显示中文）
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
        clean_name_display = no_emoji_name[:35] + "..." if len(no_emoji_name) > 35 else no_emoji_name

    if is_vertical:
        # 竖屏文字布局
        white_area_y_start = grid_h
        left_margin = 20
        right_margin = 20
        right_align_x = canvas_w - right_margin

        # 绘制左侧文字 ("TOP.num")
        top_y = white_area_y_start + 180
        draw.text((left_margin, top_y), top_text, font=top_font, fill="black", anchor="ls")
        #top_bbox = draw.textbbox((left_margin+30, top_y), top_text, font=top_font, anchor="ls")
        #num_x = top_bbox[2]
        #draw.text((num_x, top_y), num_text, font=num_font, fill="black", anchor="ls")

        # 绘制右侧文字 (日期, 时长, 标题)
        date_y = white_area_y_start + 60
        #draw.text((right_align_x, date_y), date_text, font=small_font, fill="black", anchor="ra")
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
        #top_bbox = draw.textbbox((left_margin, top_y), top_text, font=top_font, anchor="ls")
        #num_x = top_bbox[2]
        #draw.text((num_x, top_y), num_text, font=num_font, fill="black", anchor="ls")

        # 绘制右侧文字 (日期, 时长, 标题)
        date_y = white_area_y_start + 40
        #draw.text((right_align_x, date_y), date_text, font=small_font, fill="black", anchor="ra")
        duration_y = date_y + 60
        draw.text((right_align_x, duration_y), duration_text, font=small_font, fill="black", anchor="ra")
        title_y = duration_y + 80
        draw.text((right_align_x, title_y), clean_name_display, font=large_font, fill="black", anchor="ra")

    canvas.convert("RGB").save(thumb_path, 'JPEG', quality=85)
    return thumb_path



def stitch_cover(count, thumb_path, cover_path):
    thumb_img = None
    cover_img = None
    try:
        thumb_img = Image.open(thumb_path)
        cover_img = Image.open(cover_path)

        # 定义目标封面尺寸
        target_size = (1080, 1920) if count == 4 else (1920, 1080)
        tw, th = target_size

        # 1. 高斯模糊背景（完全铺满，无黑边）
        bg = ImageOps.fit(cover_img, target_size, method=Image.Resampling.LANCZOS)
        bg_small = bg.resize((tw // 10, th // 10), Image.Resampling.NEAREST)
        bg_small = bg_small.filter(ImageFilter.GaussianBlur(radius=5))
        processed_cover = bg_small.resize((tw, th), Image.Resampling.BILINEAR)

        # 2. 计算居中缩放后的前景尺寸
        orig_w, orig_h = cover_img.size
        scale = min(tw / orig_w, th / orig_h)  # 缩小到能完全显示
        new_w = int(orig_w * scale)
        new_h = int(orig_h * scale)
        fg_resized = cover_img.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # 3. 计算居中坐标
        paste_x = (tw - new_w) // 2
        paste_y = (th - new_h) // 2

        # 4. 把清晰原图贴到模糊背景上
        processed_cover.paste(fg_resized, (paste_x, paste_y))


        if count == 6:  # 横屏上下拼
            canvas = Image.new('RGB', (1920, 2160), (255, 255, 255))
            canvas.paste(thumb_img, (0, 1080))
            canvas.paste(processed_cover, (0, 0))
        else:  # 竖屏左右拼
            canvas = Image.new('RGB', (2160, 1920), (255, 255, 255))
            canvas.paste(thumb_img, (0, 0))
            canvas.paste(processed_cover, (1080, 0))

        canvas.save(thumb_path, 'JPEG', quality=85)
    except Exception as e:
        logger.error(f"stitch_cover 拼接失败: {e}")
    finally:
        if thumb_img is not None:
            thumb_img.close()
        if cover_img is not None:
            cover_img.close()


def write_text_on_image(image_path, top: int, date: str):
    """
    专门处理拼接后大图的文字写入逻辑
    """
    try:
        with Image.open(image_path) as img:
            img = img.convert("RGBA")  # 转换以支持绘制
            w, h = img.size

            # 1. 识别拼接类型
            if w == 2160 and h == 1920:
                # 竖屏视频拼接：预览图(1080x1920)在左，封面在右
                is_vertical = True
                offset_y = 0
                canvas_w = 1080  # 文字工作的逻辑宽度
            elif w == 1920 and h == 2160:
                # 横屏视频拼接：封面在上，预览图(1920x1080)在下
                is_vertical = False
                offset_y = 1080  # 文字区域下移一个封面的高度
                canvas_w = 1920
            else:
                logger.error(f"非预期的拼接尺寸: {w}x{h}")
                return False

            # 2. 准备字体与绘制对象
            draw = ImageDraw.Draw(img)
            try:

                font_file = str(Path(__file__).resolve().parent.parent / "SimHei.ttf")
                top_font = ImageFont.truetype(font_file, size=130 if is_vertical else 200)
                num_font = ImageFont.truetype(font_file, size=100 if is_vertical else 160)
                small_font = ImageFont.truetype(font_file, size=35 if is_vertical else 45)
            except IOError:
                top_font = num_font = small_font = ImageFont.load_default()

            # 3. 设置基础坐标
            grid_h = 1920 - 384 if is_vertical else 1080 - 360  # 计算宫格底部边界
            white_area_y_start = grid_h + offset_y

            top_text = "TOP"
            num_text = f".{top}"
            date_text = f"Date: {date}"

            # 4. 分别执行横竖屏绘制
            if is_vertical:
                left_margin = 20
                right_margin = 20
                right_align_x = canvas_w - right_margin

                # 绘制 .num (紧跟在 TOP 后面)
                top_y = white_area_y_start + 180
                # 需要计算 TOP 的宽度来确定 .num 的起点
                top_bbox = draw.textbbox((left_margin, top_y), top_text, font=top_font, anchor="ls")
                num_x = top_bbox[2]
                draw.text((num_x, top_y), num_text, font=num_font, fill="black", anchor="ls")

                # 绘制日期
                date_y = white_area_y_start + 60
                draw.text((right_align_x, date_y), date_text, font=small_font, fill="black", anchor="ra")
            else:
                left_margin = 40
                right_margin = 80
                right_align_x = canvas_w - right_margin

                # 绘制 .num
                top_y = white_area_y_start + 240
                top_bbox = draw.textbbox((left_margin, top_y), top_text, font=top_font, anchor="ls")
                num_x = top_bbox[2]
                draw.text((num_x, top_y), num_text, font=num_font, fill="black", anchor="ls")

                # 绘制日期
                date_y = white_area_y_start + 40
                draw.text((right_align_x, date_y), date_text, font=small_font, fill="black", anchor="ra")

            # 5. 生成新文件名并保存结果
            p = Path(image_path)
            # 这里做一个安全的替换，将斜杠替换为横杠
            safe_date = date.replace('/', '-')

            new_filename = f"{p.stem}_{safe_date}{p.suffix}"
            new_image_path = str(p.parent / new_filename)

            img.convert("RGB").save(new_image_path, 'JPEG', quality=85)
            return new_image_path
    except Exception as e:
        logger.error(f"写入文字失败: {e}")
        return False

async def generate_thumbnail(t_video_path: str, thumb_path: str,cover_path, vid_id,num, today, clean_name):
    if t_video_path == 0:
        return 0
    # 1. 创建唯一的临时工作目录，彻底避免并发冲突
    job_id = uuid.uuid4().hex
    temp_dir = Path("temp") / f"thumb_{job_id}"
    temp_dir.mkdir(parents=True,exist_ok=True)
    thumb_id = os.path.join(thumb_path, f"{vid_id}.jpg")
    cover_id = os.path.join(cover_path, f"{vid_id}.jpg")
    mini_thumb_id = os.path.join(cover_path, f"{vid_id}_thumb.jpg")

    os.makedirs(thumb_path, exist_ok=True)
    os.makedirs(cover_path, exist_ok=True)

    try:
        # 2. 异步获取视频信息
        duration, width, height , duration_str= await get_video_info_async(t_video_path)
        is_vertical = width < height

        # 3. 计算抽帧时间点
        count = 4 if is_vertical else 6
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

        if os.path.exists(cover_id):
            await generate_mini_thumb_async(cover_id, mini_thumb_id)


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

