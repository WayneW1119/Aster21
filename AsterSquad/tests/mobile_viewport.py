"""Exercise keyboard resize AND viewport pan; headless browsers have no OS keyboard."""
from playwright.sync_api import expect


VIEWPORT_MOCK = """(()=>{
 const viewport=new EventTarget();
 Object.defineProperties(viewport,{
  height:{get:()=>viewport.testHeight??innerHeight},
  width:{get:()=>innerWidth},offsetTop:{get:()=>viewport.testTop||0},
  scale:{get:()=>viewport.testScale||1}
 });
 viewport.set=(height,top=0,scale=1)=>{
  viewport.testHeight=height;viewport.testTop=top;viewport.testScale=scale;
  viewport.dispatchEvent(new Event('resize'));viewport.dispatchEvent(new Event('scroll'));
 };
 Object.defineProperty(window,'visualViewport',{value:viewport,configurable:true});
})();"""


def viewport(page, height=None, top=0, scale=1):
    page.evaluate("([h,t,s])=>visualViewport.set(h,t,s)", [height, top, scale])
    page.wait_for_timeout(100)  # viewport layout and focus visibility use animation frames


def assert_bounds(locator, top, bottom):
    rect = locator.bounding_box()
    assert rect and rect['y'] >= top - 1 and rect['y'] + rect['height'] <= bottom + 1, rect


def verify_squad_keyboard(page):
    page.locator('#newAgent').tap()
    page.locator('#agentHandle').fill('keyboard_probe')
    assert page.locator('body').evaluate('el=>getComputedStyle(el).transform') == 'none'
    viewport(page, 420, 110)
    assert_bounds(page.locator('#agentDialog'), 110, 530)
    expect(page.locator('#agentHandle')).to_be_focused()
    # Fields below the fold reveal inside the modal, without moving the app shell.
    page.locator('#agentInstruction').focus()
    page.locator('#agentInstruction').fill('输入时窗口应保持稳定。')
    page.wait_for_timeout(100)
    assert_bounds(page.locator('#agentInstruction'), 110, 530)
    before = page.locator('#agentDialog .dialog-header').bounding_box()
    page.locator('#agentDialog').evaluate('el=>el.scrollTop+=180')
    after = page.locator('#agentDialog .dialog-header').bounding_box()
    assert abs(after['y'] - before['y']) <= 1, (before, after)
    page.get_by_role('button', name='添加可读写路径', exact=True).tap()
    field = page.get_by_role('textbox', name='可读写路径', exact=True)
    field.fill('/tmp/a-long-mobile-keyboard-permission-path')
    page.wait_for_timeout(100)
    assert_bounds(field, 110, 530)
    assert page.evaluate('scrollY===0')
    viewport(page, 390, 150)
    assert_bounds(page.locator('#agentDialog'), 150, 540)
    viewport(page)
    page.locator('[data-close="agentDialog"]').first.tap()
    assert_bounds(page.locator('.workspace>.topbar'), 0, 844)
    assert_bounds(page.locator('.mobile-tabs'), 0, 844)


def verify_member_modal(page):
    """An existing member sheet must not shade or intercept its edit dialog."""
    # iOS overflow scrollers may establish a stacking context. Model that
    # explicitly: Chromium alone otherwise misses the covered member sheet.
    isolation = page.add_style_tag(content='.workspace>main{isolation:isolate}')
    panel = page.locator('#detailPanel')
    def assert_sheet_hit():
        page.wait_for_function('''()=>{
          const el=document.getElementById('detailPanel');
          const r=el.getBoundingClientRect();
          return el.contains(document.elementFromPoint(r.x+100,r.y+80));
        }''', timeout=5000)

    assert_sheet_hit()
    # A real touch swipe must move the sheet, while the background stays put.
    panel.evaluate('el=>el.scrollTop=0')
    rect = panel.bounding_box()
    cdp = page.context.new_cdp_session(page)
    x, y = rect['x'] + rect['width'] / 2, rect['y'] + rect['height'] * .8
    background_scroll = page.locator('.workspace>main').evaluate('el=>el.scrollTop')
    cdp.send('Input.dispatchTouchEvent', {'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
    for step in range(1, 13):
        cdp.send('Input.dispatchTouchEvent', {'type':'touchMove','touchPoints':[{'x':x,'y':y-step*10}]})
        page.wait_for_timeout(16)
    cdp.send('Input.dispatchTouchEvent', {'type':'touchEnd','touchPoints':[]})
    page.wait_for_timeout(200)
    assert panel.evaluate('el=>el.scrollTop>30'), 'touch swipe did not scroll the sheet'
    assert page.locator('.workspace>main').evaluate('el=>el.scrollTop') == background_scroll
    page.locator('#closeDetail').tap()
    expect(panel).not_to_be_visible()
    page.locator('#memberTable tbody tr').first.tap()
    assert_sheet_hit()
    # The same panel must survive desktop/phone layout changes.
    page.set_viewport_size({'width':1600,'height':1000})
    expect(page.locator('.workbench>#detailPanel')).to_be_visible()
    expect(page.locator('#detailBackdrop')).not_to_be_visible()
    page.set_viewport_size({'width':390,'height':844})
    expect(page.locator('#app>#detailPanel')).to_be_visible()
    assert_sheet_hit()
    expect(page.locator('#detailBackdrop')).to_be_visible()
    page.locator('#editAgent').tap()
    expect(page.locator('#detailBackdrop')).not_to_be_visible()
    assert page.locator('#agentDialog').evaluate('el=>el.matches(":modal")')
    assert page.locator('#agentDialog').evaluate(
        'el=>getComputedStyle(el,"::backdrop").backdropFilter') == 'none'
    page.locator('#agentInstruction').tap()
    expect(page.locator('#agentInstruction')).to_be_focused()
    viewport(page, 420, 110)
    assert_bounds(page.locator('#agentDialog'), 110, 530)
    # Hit testing matters: a visible dialog can still be blocked by a scrim.
    field = page.locator('#agentInstruction')
    assert field.evaluate('''el=>{
      const r=el.getBoundingClientRect();
      return document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)===el;
    }''')
    viewport(page)
    page.locator('[data-close="agentDialog"]').first.tap()
    expect(page.locator('#detailBackdrop')).to_be_visible()
    assert_sheet_hit()
    # Outside taps dismiss only the sheet; the next selection remains usable.
    page.locator('#detailBackdrop').tap(position={'x':10,'y':20})
    expect(panel).not_to_be_visible()
    page.locator('#memberTable tbody tr').first.tap()
    assert_sheet_hit()
    isolation.evaluate('el=>el.remove()')


def verify_relay_keyboard(page):
    page.locator('#text').focus()
    page.locator('#messages').evaluate('el=>el.scrollTop=el.scrollHeight')
    viewport(page, 420, 100)
    assert_bounds(page.locator('.topbar'), 100, 520)
    assert_bounds(page.locator('#composer'), 100, 520)
    assert_flush_bottom(page, 520)
    assert page.locator('#messages').evaluate('el=>el.scrollHeight-el.scrollTop-el.clientHeight<3')
    original = page.locator('#messages').inner_html()
    try:
        page.locator('#messages').evaluate("""el=>{
          el.innerHTML=Array.from({length:40},(_,i)=>'<p style="height:70px">历史消息 '+i+'</p>').join('');
          el.scrollTop=250;
        }""")
        viewport(page, 420, 140)
        assert_flush_bottom(page, 560)
        assert page.locator('#messages').evaluate('el=>Math.abs(el.scrollTop-250)<2')
        before = page.locator('#composer').bounding_box()
        bounds = page.locator('#messages').bounding_box()
        cdp = page.context.new_cdp_session(page)
        x, y = bounds['x'] + 100, bounds['y'] + bounds['height'] * .8
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': x, 'y': y}]})
        for step in range(1, 13):
            cdp.send('Input.dispatchTouchEvent', {'type': 'touchMove', 'touchPoints': [{'x': x, 'y': y - step * 8}]})
            page.wait_for_timeout(16)
        cdp.send('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})
        page.wait_for_timeout(200)
        after = page.locator('#composer').bounding_box()
        assert abs(before['y'] - after['y']) <= 1, (before, after)
        assert page.locator('#messages').evaluate('el=>el.scrollTop>260')
        assert page.evaluate('scrollY===0')
        # Pinch zoom must not reflow the shell to the zoomed viewport height.
        viewport(page, 200, 40, 2)
        assert page.evaluate('document.documentElement.style.getPropertyValue("--app-height")==="420px"')
        viewport(page)
    finally:
        page.locator('#messages').evaluate('(el,html)=>{el.innerHTML=html;el.scrollTop=el.scrollHeight}', original)
    assert_bounds(page.locator('#composer'), 0, 844)
    assert_flush_bottom(page, 844)
    # A final Safari viewport change can arrive without a resize/scroll event.
    page.locator('#text').blur()
    page.locator('#text').focus()
    page.wait_for_timeout(140)
    page.evaluate('visualViewport.testHeight=360;visualViewport.testTop=75')
    page.wait_for_timeout(900)
    assert_flush_bottom(page, 435)
    viewport(page)


def assert_flush_bottom(page, expected):
    for selector in ('body', '#app', '.main', '#composer'):
        rect = page.locator(selector).bounding_box()
        assert rect and abs(rect['y'] + rect['height'] - expected) <= 1, (selector, rect, expected)
