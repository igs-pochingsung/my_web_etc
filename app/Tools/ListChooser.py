# -*- coding: utf-8 -*-

class ListChooser():
    def __init__(self, instance_lst, name_lst=None, pre_show=None, suf_show=None):
        self.pre_show = pre_show if pre_show is not None else 'Choose:'
        self.suf_show = suf_show if pre_show is not None else '(input Number):'
        self.instance_lst = []
        self.name_lst = []
        self.add_many_element(instance_lst, name_lst)

    def add_element(self, elem, name=None):
        self.instance_lst.append(elem)
        while len(self.instance_lst) > len(self.name_lst)+1:
            self.name_lst.append(None)
        self.name_lst.append(name)

    def add_many_element(self, elem, name=None):
        if not isinstance(elem, list):
            self.instance_lst.append(elem)
            if not isinstance(name, str):
                print("[ListChooser]name should be str")
                name = None
            self.name_lst.append(name)
        else:
            self.instance_lst.extend(elem)
            nl = []
            for c, n in enumerate(elem):
                if not isinstance(name, list):
                    nl.append(None)
                    continue
                if c < len(name) and isinstance(name[c], str):
                    nl.append(name[c])
                    continue
                nl.append(None)
            self.name_lst.extend(nl)

    def del_element(self, elem):
        if elem in self.instance_lst:
            i = self.instance_lst.index(elem)
            self.instance_lst.remove(elem)
            self.name_lst.remove(self.name_lst[i])

    def show(self):
        for c, n in enumerate(self.instance_lst):
            if c < len(self.name_lst) and self.name_lst[c] is not None:
                n = self.name_lst[c]
            print("{}: {}".format(c, n))

    def choose(self):
        if self.pre_show is not None:
            print(self.pre_show)
        self.show()
        if self.suf_show is not None:
            print(self.suf_show)
        r = raw_input()
        while not r.isdigit() or int(r) >= len(self.instance_lst):
            print("please input number(0~{})".format(len(self.instance_lst)-1))
            r = raw_input()
        return self.instance_lst[int(r)]

if __name__ == "__main__":
    eleml = ["a", "b", "c", "d", "e"]
    namel = ["YO","BLA"]
    lc = ListChooser(eleml,namel)
    lc.add_element("F", "f")
    lc.add_many_element(["GGG","666","werh"],["g",None,897,"fffff"])
    # print lc.instance_lst
    # print lc.name_lst
    # lc.show()
    print(lc.choose())
