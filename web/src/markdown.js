import { config } from "md-editor-v3";

const IMAGE = /^(https?:\/\/\S+\.(?:png|jpe?g|gif|webp)(?:\?\S*)?|\/api\/feature-img\/[0-9a-f]{16}\.(?:png|jpe?g|gif|webp))$/i;

function imageToken(state, src) {
  const token = new state.Token("image", "img", 0);
  token.attrs = [["src", src], ["alt", ""]];
  token.children = [];
  return token;
}

config({
  markdownItConfig(md) {
    md.core.ruler.after("inline", "bare-image-url", (state) => {
      for (const block of state.tokens) {
        if (block.type !== "inline" || !block.children) continue;
        const kids = block.children;
        const out = [];
        for (let i = 0; i < kids.length; i++) {
          const open = kids[i];
          const text = kids[i + 1];
          const close = kids[i + 2];
          const href = open.type === "link_open" ? open.attrGet("href") || "" : "";
          if (
            href && close && close.type === "link_close" && text && text.type === "text"
            && IMAGE.test(href) && text.content.trim() === href
          ) {
            out.push(imageToken(state, href));
            i += 2;
            continue;
          }
          if (open.type === "text" && IMAGE.test(open.content.trim())) {
            out.push(imageToken(state, open.content.trim()));
            continue;
          }
          out.push(open);
        }
        block.children = out;
      }
    });
  },
});
