"""Compact, bounded print blocks; reuse the existing Arabic/math engine.

No global drawing settings are modified. One bounded canvas is alive at a time.
Unsupported or over-wide formulae fail explicitly, never become plain text.
"""
import io
from services import image_quiz_renderer as r

WIDTH = 1500
PAD = 28
SIZE = 19
MAX_HEIGHT = 1750


def _lines(text, is_ar, width, bold=False):
    font = r._font_prop_sized(is_ar, bold=bold, size=SIZE)
    text = r._auto_wrap_bare_latex(str(text))
    if text.count('$') % 2:
        raise ValueError('Unbalanced math delimiters')
    lines = [line for paragraph in text.splitlines() or [text]
             for line in r._wrap_and_shape(paragraph, is_ar, width, font)]
    result = []
    for line in lines:
        m = r._MATH_PARSER.parse(line, dpi=r.DPI, prop=font)
        if m.width > width:
            raise ValueError('Formula exceeds printable width')
        result.append((line, max(46, (m.height + m.depth) * 1.3), bold))
    return result


def _validate_cells(obj):
    if isinstance(obj, dict):
        for value in obj.values():
            _validate_cells(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _validate_cells(value)
    elif isinstance(obj, str):
        if obj.count("$") % 2:
            raise ValueError("Unbalanced math delimiters")
        for span in r._MATH_SPAN_RE.findall(obj):
            r._MATH_PARSER.parse(span, dpi=r.DPI)


def _layout(q, is_ar):
    width = WIDTH - 2 * PAD
    items = [('text', h, [(PAD, line, bold)])
             for line, h, bold in _lines(q.get('question', ''), is_ar, width, True)]
    # Validate all formulas before legacy table/matrix helpers can sanitize them.
    for obj in (q.get('table'), q.get('matrices')):
        _validate_cells(obj)
    table = r._prepare_table(q.get('table'), is_ar, width)
    if table:
        font = r._font_prop_sized(is_ar, bold=False, size=r.TABLE_FONT_SIZE)
        for row in [table['header_lines']] + table['row_lines']:
            for cell in row:
                for line, _height in cell:
                    if r._MATH_PARSER.parse(line, dpi=r.DPI, prop=font).width > table['col_width'] - 2 * r.TABLE_CELL_PAD_X:
                        raise ValueError('Table formula exceeds column width')
        items.append(('table', table['total_h'] + 20, table))
    matrices = r._prepare_matrices(q.get('matrices'), is_ar) or []
    for matrix in matrices:
        if matrix['total_w'] > width:
            raise ValueError('Matrix exceeds printable width')
        items.append(('matrix', matrix['grid_h'] + 35, [matrix]))
    opts = q.get('options') or []
    labels = q.get('_print_labels') or r.letters_for(is_ar, len(opts))
    raw = [f'{letter}) {opt}' for letter, opt in zip(labels, opts)]
    # Measure at each candidate width, never shrink the font to force a grid.
    blocks = None
    columns = 1
    for n in (4, 2):
        if len(raw) < n:
            continue
        try:
            candidate = [_lines(s, is_ar, width / n - 45) for s in raw]
            if all(len(b) == 1 for b in candidate):
                columns, blocks = n, candidate
                break
        except ValueError:
            pass
    if blocks is None:
        blocks = [_lines(s, is_ar, width - 45) for s in raw]
    rows = []
    for start in range(0, len(blocks), columns):
        row = blocks[start:start + columns]
        row_h = max(sum(line[1] for line in cell) for cell in row) + 32
        rows.append((row_h, row))
    # Keep a normal 2x2 grid together. Very long options continue in bounded rows.
    group, group_h = [], 0
    for row_h, row in rows:
        if row_h > MAX_HEIGHT - 150 and columns == 1:
            pieces, piece_h = [], 0
            for line in row[0]:
                if pieces and piece_h + line[1] > MAX_HEIGHT - 182:
                    if group:
                        items.append(('options', group_h + 20, (columns, group)))
                        group, group_h = [], 0
                    items.append(('options', piece_h + 52, (1, [(piece_h + 32, [pieces])])))
                    pieces, piece_h = [], 0
                pieces.append(line)
                piece_h += line[1]
            row_h, row = piece_h + 32, [pieces]
        if group and group_h + row_h > MAX_HEIGHT - 150:
            items.append(('options', group_h + 20, (columns, group)))
            group, group_h = [], 0
        group.append((row_h, row))
        group_h += row_h
    if group:
        items.append(('options', group_h + 20, (columns, group)))

    return items


def _draw_options(ax, data, y_top, is_ar):
    columns, rows = data
    width = WIDTH - 2 * PAD
    cell_w = width / columns
    height = sum(row_h for row_h, _ in rows)
    top = y_top - 10
    ax.add_patch(r.Rectangle((PAD, top - height), width, height,
                            fill=False, edgecolor='black', linewidth=0.65))
    for col in range(1, columns):
        x = PAD + col * cell_w
        ax.plot([x, x], [top, top - height], color='black', linewidth=0.65)
    y = top
    for ri, (row_h, cells) in enumerate(rows):
        if ri:
            ax.plot([PAD, WIDTH - PAD], [y, y], color='black', linewidth=0.65)
        for col, lines in enumerate(cells):
            visual_col = columns - 1 - col if is_ar else col
            x = PAD + (visual_col + 0.5) * cell_w
            line_y = y - (row_h - sum(h for _, h, _ in lines)) / 2
            for line, line_h, bold in lines:
                ax.text(x, line_y - line_h / 2, line, ha='center', va='center',
                        fontsize=SIZE, fontproperties=r._font_prop(is_ar, bold), color='black')
                line_y -= line_h
        y -= row_h


def iter_print_blocks(q, is_ar, heading):
    # Guard input before measuring to bound even malformed imported quizzes.
    if len(str(q)) > 40000:
        raise ValueError('Question too large for print export')
    with r._RENDER_LOCK:
        items = _layout(q, is_ar)
    chunks, chunk, height = [], [], 0
    for item in items:
        if item[1] > MAX_HEIGHT - 130:
            raise ValueError('Table or matrix too tall for a printed page')
        if chunk and height + item[1] > MAX_HEIGHT - 130:
            chunks.append(chunk)
            chunk, height = [], 0
        chunk.append(item)
        height += item[1]
    if chunk:
        chunks.append(chunk)
    for index, chunk in enumerate(chunks):
        with r._RENDER_LOCK:
            h = sum(item[1] for item in chunk) + 130
            fig = r.Figure(figsize=(WIDTH / r.DPI, h / r.DPI), dpi=r.DPI)
            canvas = r.FigureCanvasAgg(fig)
            ax = fig.add_axes((0, 0, 1, 1))
            ax.axis('off')
            ax.set_xlim(0, WIDTH)
            ax.set_ylim(0, h)
            align = 'right' if is_ar else 'left'
            label = heading + ((' — تابع' if is_ar else ' — continued') if index else '')
            ax.text(WIDTH - PAD if is_ar else PAD, h - 12,
                    r._shape_line(label) if is_ar else label, ha=align, va='top',
                    fontsize=SIZE, fontproperties=r._font_prop(is_ar, True), color='black')
            y = h - 90
            for kind, ih, data in chunk:
                if kind == 'text':
                    for offset, line, bold in data:
                        ax.text(WIDTH - offset if is_ar else offset, y, line,
                                ha=align, va='top', fontsize=SIZE,
                                fontproperties=r._font_prop(is_ar, bold), color='black')
                elif kind == 'options':
                    _draw_options(ax, data, y, is_ar)
                elif kind == 'table':
                    r._draw_table(ax, data, PAD, y, WIDTH - 2 * PAD, is_ar)
                else:
                    r._draw_matrices(ax, data, PAD, y, WIDTH - 2 * PAD, is_ar)
                y -= ih
            with io.BytesIO() as buf:
                fig.savefig(buf, format='png', facecolor='white')
                payload = buf.getvalue()
            fig.clear()
            del fig, canvas, ax
        yield payload
        del payload
        from mem_utils import release_memory_to_os
        release_memory_to_os()
