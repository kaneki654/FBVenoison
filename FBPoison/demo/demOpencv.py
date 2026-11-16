import uiautomator2 as tite
import time
import xml.dom.minidom as mamamo
d = tite.connect()
pn = "com.facebook.lite"
def dt(d):
    for el in d(className="android.widget.TextView"):
        t = el.info["text"]
        if t:
            print(repr(t))
def dd(d):
    xml = mamamo.parseString(d.dump_hierarchy()).toprettyxml()
    for line in xml.splitlines():
        if "content-desc" in line and 'content-desc=""' not in line:
            print(line.strip())
def tg(d):
    for desc in ("Settings", "Settings and Privacy", "Options", "Menu"):
        g = d(description=desc)
        if g.exists:
            g.click()
            return True
    W, H = d.window_size()
    best = None
    for el in d(clickable=True):
        b = el.info.get("bounds")
        if not b:
            continue
        if b["top"] < H * 0.15 and b["left"] > W * 0.6:
            best = b
        if best:
            cx = (best["left"] + best["right"]) // 2
            cy = (best["top"] + best["bottom"]) // 2
            print(f"gear at ({cx}, {cy})")
            d.click(cx, cy)
            return True
        print("gear not found")
        return False
def tl(d):
    labels = []
    for el in d(className="android.widget.TextView"):
        t = el.info["text"]
        if t:
            labels.append(t)
    return labels
def main():
    try:
        d.app_start(pn)
        labels = tl(d)
        print("labels:", labels)
        d.app_stop(pn)
    except Exception as e:
        print("Error:", e)
    except AndroidConnectionError as ace:
        print("Connection error:", ace)
    except uiautomator2.exceptions.UiObjectNotFoundError as unfe:
        print("UI object not found:", unfe)
    except uiautomator2.exceptions.AndroidDeviceError as ade:
        print("Android device error:", ade)
    except uiautomator2.exceptions.AndroidNotFoundError as anfe:
        print("Android not found:", anfe)
    except KeyboardInterrupt:
        print("Interrupted by user")
if __name__ == "__main__":
    main()
